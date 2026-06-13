"""Resale intelligence — eBay sold-listing prices.

Primary source is the eBay Finding API (`findCompletedItems`, sold only), the
cleanest legal source of real transaction data. When `EBAY_APP_ID` is not set —
or any call fails — it falls back to bundled fixtures so the product still has
defensible numbers offline.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from .. import db
from ..config import FIXTURES_DIR, Settings
from ..stats import resale_summary
from ..util import iso, now_utc

_FIXTURES_CACHE: dict | None = None


def _fixtures() -> dict:
    global _FIXTURES_CACHE
    if _FIXTURES_CACHE is None:
        _FIXTURES_CACHE = json.loads((FIXTURES_DIR / "ebay_sold.json").read_text())
    return _FIXTURES_CACHE


def _g(product: Any, key: str, default: Any = None) -> Any:
    try:
        return product[key]
    except (KeyError, IndexError, TypeError):
        return getattr(product, key, default)


def _fetch_ebay_sold(settings: Settings, product: Any, *, client: Any | None = None) -> list[float]:
    """Query the eBay Finding API for recent sold prices. Returns [] on failure."""
    keywords = f"{_g(product, 'brand', '')} {_g(product, 'name', '')}".strip()
    params = {
        "OPERATION-NAME": "findCompletedItems",
        "SERVICE-VERSION": "1.13.0",
        "SECURITY-APPNAME": settings.ebay_app_id,
        "RESPONSE-DATA-FORMAT": "JSON",
        "REST-PAYLOAD": "",
        "keywords": keywords,
        "itemFilter(0).name": "SoldItemsOnly",
        "itemFilter(0).value": "true",
        "paginationInput.entriesPerPage": "50",
    }
    url = "https://svcs.ebay.com/services/search/FindingService/v1"
    try:
        if client is not None:
            data = client.get(url, params=params).json()
        else:
            import httpx
            with httpx.Client(timeout=20.0) as c:
                data = c.get(url, params=params).json()
        items = (
            data["findCompletedItemsResponse"][0]["searchResult"][0].get("item", [])
        )
        prices: list[float] = []
        for it in items:
            selling = it.get("sellingStatus", [{}])[0]
            sold = selling.get("sellingState", [""])[0] == "EndedWithSales"
            price = selling.get("convertedCurrentPrice", [{}])[0].get("__value__")
            if sold and price:
                prices.append(float(price))
        return prices
    except Exception:
        return []


def fetch_prices(settings: Settings, product: Any, *, client: Any | None = None) -> tuple[list[float], str]:
    """Return (prices, source). Tries eBay if configured, else fixtures."""
    if settings.ebay_app_id:
        prices = _fetch_ebay_sold(settings, product, client=client)
        if prices:
            return prices, "ebay"
    return [float(x) for x in _fixtures().get(_g(product, "sku"), [])], "fixture"


def refresh_product(conn: sqlite3.Connection, settings: Settings, product: Any,
                    *, client: Any | None = None) -> dict:
    """Compute and persist a fresh resale snapshot for one product."""
    prices, source = fetch_prices(settings, product, client=client)
    summary = resale_summary(prices)
    conn.execute(
        """INSERT INTO resale_prices
           (product_id, source, sample_size, low, high, median, average, currency, captured_at)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (_g(product, "id"), source, summary["sample_size"], summary["low"],
         summary["high"], summary["median"], summary["average"], "USD", iso(now_utc())),
    )
    conn.commit()
    summary["source"] = source
    return summary


def refresh_all(conn: sqlite3.Connection, settings: Settings,
                product_ids: list[int] | None = None) -> int:
    if product_ids:
        placeholders = ",".join("?" * len(product_ids))
        products = db.q(conn, f"SELECT * FROM products WHERE id IN ({placeholders})", product_ids)
    else:
        products = db.q(conn, "SELECT * FROM products")
    for p in products:
        refresh_product(conn, settings, p)
    return len(products)


def latest(conn: sqlite3.Connection, product_id: int) -> sqlite3.Row | None:
    return db.one(
        conn,
        "SELECT * FROM resale_prices WHERE product_id=? ORDER BY captured_at DESC, id DESC LIMIT 1",
        (product_id,),
    )


def previous(conn: sqlite3.Connection, product_id: int) -> sqlite3.Row | None:
    rows = db.q(
        conn,
        "SELECT * FROM resale_prices WHERE product_id=? ORDER BY captured_at DESC, id DESC LIMIT 2",
        (product_id,),
    )
    return rows[1] if len(rows) > 1 else None
