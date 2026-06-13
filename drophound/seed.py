"""Seed the database with a realistic, fully-populated demo.

Generates: the product catalog, a plausible ~10-week restock history per product
(so cadence prediction has signal), two resale snapshots per product (so trends
render), and a demo premium subscriber with a collection that shows real P/L.

Deterministic: a fixed RNG seed means re-seeding produces the same demo.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import timedelta
from random import Random

from . import db
from .config import FIXTURES_DIR, Settings
from .stats import resale_summary
from .util import iso, now_utc

RNG_SEED = 20260613

# Products that should currently be in stock (a fresh restock within ~1 day),
# mapped to how many days ago that restock happened.
FRESH = {
    "PM-LAB-MAC-01": 0.12,
    "PM-LAB-ENERGY-01": 0.30,
    "PM-SKP-SOUND-01": 0.55,
    "SA-HIPPER-01": 0.80,
    "SM-LIVING-01": 0.95,
}


def _load_json(name: str):
    return json.loads((FIXTURES_DIR / name).read_text())


def _insert_products(conn: sqlite3.Connection, catalog: list[dict]) -> dict[str, int]:
    created = now_utc()
    sku_to_id: dict[str, int] = {}
    for p in catalog:
        cur = conn.execute(
            """INSERT INTO products
               (sku, brand, line, character, name, retailer, region,
                product_url, image_hint, retail_price, currency, in_stock_signal, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                p["sku"], p["brand"], p["line"], p["character"], p["name"],
                p["retailer"], p["region"], p.get("product_url"), p.get("image_hint"),
                p.get("retail_price"), p.get("currency", "USD"),
                p.get("in_stock_signal"), iso(created),
            ),
        )
        sku_to_id[p["sku"]] = cur.lastrowid
    conn.commit()
    return sku_to_id


def _insert_history(conn: sqlite3.Connection, catalog: list[dict], sku_to_id: dict[str, int]) -> None:
    rng = Random(RNG_SEED)
    now = now_utc()

    for i, p in enumerate(catalog):
        pid = sku_to_id[p["sku"]]
        retail = p.get("retail_price")
        cadence = 9 + (i * 1.7) % 10          # 9..~19 days
        jitter = cadence * (0.08 + (i % 3) * 0.10)
        fresh_days = FRESH.get(p["sku"])
        last_days = fresh_days if fresh_days is not None else rng.uniform(2.5, 16.0)

        # Build ~6 "became available" timestamps walking backwards from last_days ago.
        t = now - timedelta(days=last_days)
        avail = [t]
        for _ in range(5):
            t = t - timedelta(days=cadence + rng.uniform(-jitter, jitter))
            avail.append(t)
        avail.sort()

        events: list[tuple] = []
        for idx, at in enumerate(avail):
            etype = "drop" if idx == 0 else "restock"
            note = "Initial drop" if idx == 0 else "Back in stock"
            events.append((etype, "in_stock", retail, "sample", note, at))
            is_last = idx == len(avail) - 1
            keep_in_stock = is_last and fresh_days is not None and fresh_days < 2
            if not keep_in_stock:
                sold_out_at = at + timedelta(days=rng.uniform(0.5, min(cadence - 1, 4)))
                if sold_out_at < now:
                    events.append(("sold_out", "sold_out", None, "sample", "Sold out", sold_out_at))

        # A couple of recent price-drop events for variety in the feed.
        if i % 5 == 2 and retail:
            pd_at = now - timedelta(hours=rng.uniform(3, 20))
            events.append(("price_drop", "in_stock", round(retail * 0.85, 2), "sample",
                           "Price drop on resale partner", pd_at))

        for etype, status, price, source, note, at in events:
            conn.execute(
                """INSERT INTO restock_events
                   (product_id, event_type, status, price, note, source, detected_at)
                   VALUES (?,?,?,?,?,?,?)""",
                (pid, etype, status, price, note, source, iso(at)),
            )
    conn.commit()


def _insert_resale(conn: sqlite3.Connection, catalog: list[dict], sku_to_id: dict[str, int]) -> None:
    sold = _load_json("ebay_sold.json")
    now = now_utc()
    week_ago = now - timedelta(days=7)
    for p in catalog:
        prices = sold.get(p["sku"])
        if not prices:
            continue
        pid = sku_to_id[p["sku"]]
        # Older snapshot ~10% lower so the dashboard shows an upward trend.
        old = resale_summary([round(x * 0.9, 2) for x in prices])
        new = resale_summary(prices)
        for summary, captured in ((old, week_ago), (new, now)):
            conn.execute(
                """INSERT INTO resale_prices
                   (product_id, source, sample_size, low, high, median, average, currency, captured_at)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (pid, "ebay", summary["sample_size"], summary["low"], summary["high"],
                 summary["median"], summary["average"], "USD", iso(captured)),
            )
    conn.commit()


def _insert_subscribers(conn: sqlite3.Connection, sku_to_id: dict[str, int]) -> None:
    now = now_utc()

    def add_sub(email, tier, telegram=None, discord=None, brands="", characters="",
                regions="", ceiling=None, premium_since=None):
        cur = conn.execute(
            """INSERT INTO subscribers
               (email, tier, telegram, discord, filter_brands, filter_characters,
                filter_regions, price_ceiling, created_at, premium_since)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (email, tier, telegram, discord, brands, characters, regions,
             ceiling, iso(now), premium_since),
        )
        return cur.lastrowid

    demo_id = add_sub(
        "demo@drophound.app", "premium", telegram="@demo_collector",
        characters="Labubu,Molly,Crybaby", regions="US", ceiling=200.0,
        premium_since=iso(now - timedelta(days=21)),
    )
    add_sub("collector@example.com", "free", telegram="@popmart_fan")
    add_sub("jane@example.com", "free")

    collection = [
        ("PM-LAB-ENERGY-01", 1, 27.99, "mint"),
        ("PM-LAB-SEAT-01", 2, 15.99, "mint"),
        ("PM-SKP-SOUND-01", 1, 16.99, "mint"),
        ("PM-MOL-CAREER-01", 1, 14.99, "opened"),
        ("SM-LIVING-01", 3, 10.50, "mint"),
        ("PM-CRY-AGAIN-01", 1, 15.99, "mint"),
    ]
    for sku, qty, paid, condition in collection:
        conn.execute(
            """INSERT INTO collection_items
               (subscriber_id, product_id, qty, paid_price, condition, acquired_at)
               VALUES (?,?,?,?,?,?)""",
            (demo_id, sku_to_id[sku], qty, paid, condition,
             iso(now - timedelta(days=30))),
        )
    conn.commit()


def seed(conn: sqlite3.Connection, settings: Settings | None = None, *, reset: bool = True) -> dict:
    """Populate the database. Returns a small summary dict."""
    db.init_db(conn)
    if reset:
        db.reset_data(conn)

    catalog = _load_json("catalog.json")
    sku_to_id = _insert_products(conn, catalog)
    _insert_history(conn, catalog, sku_to_id)
    _insert_resale(conn, catalog, sku_to_id)
    _insert_subscribers(conn, sku_to_id)

    counts = {
        "products": db.one(conn, "SELECT COUNT(*) c FROM products")["c"],
        "events": db.one(conn, "SELECT COUNT(*) c FROM restock_events")["c"],
        "resale_snapshots": db.one(conn, "SELECT COUNT(*) c FROM resale_prices")["c"],
        "subscribers": db.one(conn, "SELECT COUNT(*) c FROM subscribers")["c"],
        "collection_items": db.one(conn, "SELECT COUNT(*) c FROM collection_items")["c"],
    }
    return counts
