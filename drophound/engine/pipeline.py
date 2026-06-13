"""The orchestration cycle: observe -> record -> refresh -> alert -> log.

This is the heart of the automation stack — the code equivalent of an n8n/Make
flow. One call to `run_cycle` runs monitors, records meaningful state changes as
events, refreshes resale prices for affected items, and dispatches alerts to the
free broadcast channels plus matched premium subscribers.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Any

from .. import db, filters
from ..config import Settings
from ..stats import premium_multiple
from ..util import iso, money, now_utc
from . import resale
from .alerts import AlertMessage, broadcast_dispatchers, send_personal_email
from .monitors import Observation, SampleMonitor

BROADCAST_TYPES = {"drop", "restock", "price_drop"}


def _load_products_with_status(conn: sqlite3.Connection) -> tuple[list[dict], dict[int, dict]]:
    rows = db.q(
        conn,
        """SELECT p.*,
                  (SELECT status FROM restock_events e WHERE e.product_id=p.id
                   ORDER BY e.detected_at DESC, e.id DESC LIMIT 1) AS current_status,
                  (SELECT price FROM restock_events e WHERE e.product_id=p.id
                   ORDER BY e.detected_at DESC, e.id DESC LIMIT 1) AS current_price
           FROM products p""",
    )
    products = [dict(r) for r in rows]
    by_id = {p["id"]: p for p in products}
    return products, by_id


def _derive_event_type(obs: Observation, prev_status: str | None) -> str | None:
    """For monitors that don't set event_type, infer a meaningful change (or None)."""
    if obs.event_type:
        return obs.event_type
    if obs.status == "in_stock" and prev_status in (None, "sold_out", "low_stock", "unknown"):
        return "restock"
    if obs.status == "sold_out" and prev_status not in (None, "sold_out"):
        return "sold_out"
    if obs.status == "low_stock" and prev_status not in (None, "low_stock"):
        return "low_stock"
    return None


def _build_alert(conn: sqlite3.Connection, settings: Settings, event_id: int,
                 event_type: str, product: dict, price: float | None) -> AlertMessage:
    verb = {"drop": "DROP", "restock": "RESTOCK", "price_drop": "PRICE DROP"}.get(
        event_type, event_type.upper())
    eff_price = price if price is not None else product.get("retail_price")
    res = resale.latest(conn, product["id"])
    resale_bit = ""
    if res and res["median"]:
        mult = premium_multiple(product.get("retail_price"), res["median"])
        mult_bit = f", ~{mult}x retail" if mult else ""
        resale_bit = f" Resale median {money(res['median'])}{mult_bit}."
    url = f"{settings.base_url}/go/{product['id']}?to=popmart"
    text = (f"🔔 {verb}: {product['name']} — {money(eff_price)} at "
            f"{product['retailer']} ({product['region']}).{resale_bit}")
    return AlertMessage(title=f"{verb}: {product['name']}", text=text, url=url,
                        event_id=event_id, product_id=product["id"])


def run_cycle(conn: sqlite3.Connection, settings: Settings, *, monitor: Any | None = None,
              now: datetime | None = None, refresh_resale: bool = True,
              email_client: Any | None = None) -> dict:
    now = now or now_utc()
    monitor = monitor or SampleMonitor()
    products, by_id = _load_products_with_status(conn)

    observations = monitor.check(products)

    created: list[dict] = []
    affected: set[int] = set()
    for obs in observations:
        product = by_id.get(obs.product_id)
        if not product:
            continue
        event_type = _derive_event_type(obs, product.get("current_status"))
        if not event_type:
            continue
        cur = conn.execute(
            """INSERT INTO restock_events
               (product_id, event_type, status, price, note, source, detected_at)
               VALUES (?,?,?,?,?,?,?)""",
            (product["id"], event_type, obs.status, obs.price, obs.note,
             obs.source, iso(now)),
        )
        event_id = cur.lastrowid
        affected.add(product["id"])
        created.append({"event_id": event_id, "event_type": event_type,
                        "product": product, "price": obs.price})
    conn.commit()

    refreshed = 0
    if refresh_resale and affected:
        refreshed = resale.refresh_all(conn, settings, list(affected))

    dispatchers = broadcast_dispatchers(settings)
    premium_subs = db.q(conn, "SELECT * FROM subscribers WHERE tier='premium'")

    broadcasts = 0
    premium_matches = 0
    watch_matches = 0
    personal_emails = 0
    by_type: dict[str, int] = {}
    for c in created:
        by_type[c["event_type"]] = by_type.get(c["event_type"], 0) + 1
        if c["event_type"] not in BROADCAST_TYPES:
            continue
        msg = _build_alert(conn, settings, c["event_id"], c["event_type"],
                           c["product"], c["price"])

        for d in dispatchers:
            result = d.send(msg)
            conn.execute(
                """INSERT INTO alerts_log (event_id, subscriber_id, channel, status, detail, created_at)
                   VALUES (?,?,?,?,?,?)""",
                (c["event_id"], None, result.channel, result.status, result.detail, iso(now)),
            )
            broadcasts += 1

        # Targeted subscribers: premium filter matches + per-product watchers.
        # recipients maps subscriber_id -> {email, reason} (deduped, watch wins).
        recipients: dict[int, dict] = {}
        for sub in premium_subs:
            if filters.matches(sub, c["product"], price=c["price"]):
                conn.execute(
                    """INSERT INTO alerts_log (event_id, subscriber_id, channel, status, detail, created_at)
                       VALUES (?,?,?,?,?,?)""",
                    (c["event_id"], sub["id"], "premium", "dry_run",
                     f"matched filters for {sub['email']}", iso(now)),
                )
                premium_matches += 1
                recipients.setdefault(sub["id"], {"email": sub["email"], "reason": "filter"})

        for w in db.q(
            conn,
            """SELECT w.subscriber_id, s.email FROM watchlist w
               JOIN subscribers s ON s.id = w.subscriber_id WHERE w.product_id = ?""",
            (c["product"]["id"],),
        ):
            conn.execute(
                """INSERT INTO alerts_log (event_id, subscriber_id, channel, status, detail, created_at)
                   VALUES (?,?,?,?,?,?)""",
                (c["event_id"], w["subscriber_id"], "watch", "dry_run",
                 f"watched by {w['email']}", iso(now)),
            )
            watch_matches += 1
            recipients[w["subscriber_id"]] = {"email": w["email"], "reason": "watch"}

        # Per-person email delivery: each subscriber gets an email for just their item.
        if settings.resend_api_key:
            for sid, info in recipients.items():
                if not info["email"]:
                    continue
                result = send_personal_email(settings, info["email"], msg,
                                             reason=info["reason"], client=email_client)
                conn.execute(
                    """INSERT INTO alerts_log (event_id, subscriber_id, channel, status, detail, created_at)
                       VALUES (?,?,?,?,?,?)""",
                    (c["event_id"], sid, "email", result.status,
                     f"personal -> {info['email']}", iso(now)),
                )
                if result.status == "sent":
                    personal_emails += 1
    conn.commit()

    return {
        "observations": len(observations),
        "events_created": len(created),
        "by_type": by_type,
        "broadcasts": broadcasts,
        "premium_matches": premium_matches,
        "watch_matches": watch_matches,
        "personal_emails": personal_emails,
        "resale_refreshed": refreshed,
        "events": [
            {"name": c["product"]["name"], "event_type": c["event_type"],
             "price": c["price"]}
            for c in created
        ],
    }
