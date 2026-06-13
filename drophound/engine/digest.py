"""AI layer — the daily/weekly digest writer.

Assembles the day's signal (new drops/restocks, resale movers, likely upcoming
restocks) into a digest plus a few social captions. If `ANTHROPIC_API_KEY` is
set it rewrites the body into punchy copy with Claude; otherwise a clean
deterministic template is used. Either way, content volume stays high with
near-zero human time — exactly the plan's intent.
"""

from __future__ import annotations

import sqlite3
from datetime import timedelta
from typing import Any

from .. import db
from ..config import Settings
from ..patterns import predict_restock, window_label
from ..stats import premium_multiple, trend_pct
from ..util import money, now_utc, parse_iso
from . import resale

PERIOD_HOURS = {"daily": 24, "weekly": 24 * 7}


def _recent_events(conn: sqlite3.Connection, hours: int) -> list[sqlite3.Row]:
    cutoff = now_utc() - timedelta(hours=hours)
    return db.q(
        conn,
        """SELECT e.*, p.name, p.brand, p.character, p.region, p.retailer, p.retail_price
           FROM restock_events e JOIN products p ON p.id = e.product_id
           WHERE e.detected_at >= ? AND e.event_type IN ('drop','restock','price_drop')
           ORDER BY e.detected_at DESC""",
        (cutoff.isoformat(),),
    )


def _top_movers(conn: sqlite3.Connection, limit: int = 3) -> list[dict]:
    movers = []
    for p in db.q(conn, "SELECT * FROM products"):
        cur = resale.latest(conn, p["id"])
        prev = resale.previous(conn, p["id"])
        if not cur or not prev or cur["median"] is None or prev["median"] is None:
            continue
        pct = trend_pct(prev["median"], cur["median"])
        if pct is None:
            continue
        movers.append({
            "name": p["name"],
            "character": p["character"],
            "median": cur["median"],
            "trend_pct": pct,
            "multiple": premium_multiple(p["retail_price"], cur["median"]),
        })
    movers.sort(key=lambda m: m["trend_pct"], reverse=True)
    return movers[:limit]


def _upcoming(conn: sqlite3.Connection, limit: int = 4) -> list[dict]:
    out = []
    for p in db.q(conn, "SELECT * FROM products"):
        events = db.q(
            conn,
            "SELECT event_type, detected_at FROM restock_events WHERE product_id=?",
            (p["id"],),
        )
        pred = predict_restock(events)
        if pred["confidence"] == "unknown" or pred["days_until"] is None:
            continue
        if pred["days_until"] < -2 or pred["days_until"] > 14:
            continue
        out.append({"name": p["name"], "label": window_label(pred),
                    "days_until": pred["days_until"]})
    out.sort(key=lambda x: x["days_until"])
    return out[:limit]


def _render_template(title: str, events, movers, upcoming) -> str:
    lines = [f"# {title}", ""]

    lines.append("## New drops & restocks")
    if events:
        for e in events[:8]:
            verb = {"drop": "DROP", "restock": "RESTOCK", "price_drop": "PRICE DROP"}[e["event_type"]]
            price = money(e["price"] or e["retail_price"])
            lines.append(f"- **{verb}** · {e['name']} — {price} at {e['retailer']} ({e['region']})")
    else:
        lines.append("- Quiet window — no new drops in this period.")
    lines.append("")

    lines.append("## Resale movers")
    if movers:
        for m in movers:
            mult = f" · {m['multiple']}x retail" if m["multiple"] else ""
            sign = "+" if m["trend_pct"] >= 0 else ""
            lines.append(f"- {m['name']} — median {money(m['median'])} "
                         f"({sign}{m['trend_pct']}% wk{mult})")
    else:
        lines.append("- Resale flat across tracked items.")
    lines.append("")

    lines.append("## Likely restocks ahead")
    if upcoming:
        for u in upcoming:
            lines.append(f"- {u['name']} — {u['label']}")
    else:
        lines.append("- No high-confidence windows in the next two weeks.")
    return "\n".join(lines)


def _captions(events, movers) -> list[str]:
    caps = []
    if events:
        e = events[0]
        caps.append(f"🚨 {e['character']} just dropped at {e['retailer']}. "
                    f"Link in bio before it's gone. #popmart #{e['character'].lower()}")
    if movers:
        m = movers[0]
        caps.append(f"📈 {m['name']} resale is up {m['trend_pct']}% this week "
                    f"(median {money(m['median'])}). Holders eating good.")
    caps.append("Never miss a drop again — free alerts at DropHound. 🐾")
    return caps


def _rewrite_with_claude(settings: Settings, body: str, *, client: Any | None = None) -> str | None:
    """Best-effort punch-up via the Anthropic Messages API. None on failure."""
    prompt = (
        "You are the editor for DropHound, a blind-box toy drop tracker. "
        "Rewrite the digest below as punchy, scannable copy for collectors. "
        "Keep all facts, prices, and product names exact. Keep the markdown "
        "headers. Be energetic but not cringe.\n\n" + body
    )
    payload = {
        "model": settings.digest_model,
        "max_tokens": 1200,
        "messages": [{"role": "user", "content": prompt}],
    }
    headers = {
        "x-api-key": settings.anthropic_api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    url = "https://api.anthropic.com/v1/messages"
    try:
        if client is not None:
            data = client.post(url, headers=headers, json=payload).json()
        else:
            import httpx
            with httpx.Client(timeout=60.0) as c:
                data = c.post(url, headers=headers, json=payload).json()
        parts = [b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"]
        text = "".join(parts).strip()
        return text or None
    except Exception:
        return None


def build_digest(conn: sqlite3.Connection, settings: Settings,
                 period: str = "daily", *, client: Any | None = None) -> dict:
    hours = PERIOD_HOURS.get(period, 24)
    title = f"DropHound {period.title()} Digest · {now_utc().strftime('%b %-d, %Y')}"

    events = _recent_events(conn, hours)
    movers = _top_movers(conn)
    upcoming = _upcoming(conn)

    body = _render_template(title, events, movers, upcoming)
    generated_with = "template"

    if settings.has_anthropic:
        rewritten = _rewrite_with_claude(settings, body, client=client)
        if rewritten:
            body, generated_with = rewritten, "claude"

    return {
        "title": title,
        "period": period,
        "body": body,
        "captions": _captions(events, movers),
        "generated_with": generated_with,
        "counts": {"events": len(events), "movers": len(movers), "upcoming": len(upcoming)},
    }
