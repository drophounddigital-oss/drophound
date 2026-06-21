"""Fetch current retail prices from product pages.

Works reliably for standard Shopify stores (those added via `add-product` or
`bulk-import`). The Shopify `.js` per-product endpoint returns price in cents
and live availability — no HTML scraping needed.

Pop Mart wraps their Shopify backend in a client-side Next.js shell; their `.js`
endpoint serves HTML, not JSON. Pop Mart prices must be updated manually in
catalog.json or via a headless browser runner.

Usage:
    python -m drophound refresh-prices
    python -m drophound refresh-prices --dry-run
"""

from __future__ import annotations

import logging

import httpx

from .. import db
from ..config import Settings

logger = logging.getLogger("drophound.price_fetch")

_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; DropHound/1.0; price-refresh)"}

# Domains that serve HTML from their .js endpoint (client-side frontends).
# Price scraping won't work for these without a headless browser.
_UNSUPPORTED_DOMAINS = ("popmart.com",)


def _is_unsupported(url: str) -> bool:
    return any(d in url for d in _UNSUPPORTED_DOMAINS)


def _shopify_price(client: httpx.Client, product_url: str) -> tuple[float | None, bool | None]:
    """Return (price_dollars, available) from a Shopify product .js endpoint.

    The .js endpoint reports price in integer cents. Returns (None, None) if the
    endpoint is unavailable or returns HTML instead of JSON.
    """
    url = product_url.rstrip("/") + ".js"
    try:
        r = client.get(url, timeout=15)
        ct = r.headers.get("content-type", "")
        if r.status_code != 200 or "json" not in ct:
            return None, None
        data = r.json()
        variants = data.get("variants") or []
        if not variants:
            return None, None
        v = variants[0]
        raw_price = v.get("price")
        available = bool(data.get("available")) or any(
            vv.get("available") for vv in variants
        )
        if raw_price is None:
            return None, available
        # .js price is in cents
        return round(float(raw_price) / 100.0, 2), available
    except Exception as exc:
        logger.debug("price fetch failed %s: %s", url, exc)
        return None, None


def refresh_prices(
    conn,
    settings: Settings,
    *,
    dry_run: bool = False,
    timeout: int = 15,
) -> list[dict]:
    """Fetch current prices for all products that have a stored product_url.

    Returns a list of result dicts, one per product:
      status: "updated" | "no_change" | "fetch_failed" | "unsupported"
      name, old_price, new_price (when fetched), reason (on failure)
    """
    products = db.q(
        conn,
        "SELECT id, name, brand, product_url, retail_price FROM products "
        "WHERE product_url IS NOT NULL AND product_url != ''",
    )

    results: list[dict] = []
    with httpx.Client(
        follow_redirects=True, timeout=timeout, headers=_HEADERS
    ) as client:
        for p in products:
            url = p["product_url"]

            if _is_unsupported(url):
                results.append({
                    "name": p["name"],
                    "status": "unsupported",
                    "old_price": p["retail_price"],
                    "new_price": None,
                    "reason": "client-side JS store (update manually in catalog.json)",
                })
                continue

            price, available = _shopify_price(client, url)

            if price is None:
                results.append({
                    "name": p["name"],
                    "status": "fetch_failed",
                    "old_price": p["retail_price"],
                    "new_price": None,
                    "reason": "endpoint did not return JSON price data",
                })
                continue

            old = p["retail_price"]
            changed = old is None or abs(price - old) >= 0.01
            if changed and not dry_run:
                db.execute(
                    conn,
                    "UPDATE products SET retail_price = ? WHERE id = ?",
                    (price, p["id"]),
                )

            results.append({
                "name": p["name"],
                "status": "updated" if (changed and not dry_run) else (
                    "dry_run_would_update" if (changed and dry_run) else "no_change"
                ),
                "old_price": old,
                "new_price": price,
                "available": available,
            })
            logger.info(
                "%s  $%.2f → $%.2f  %s",
                p["name"], old or 0, price, "updated" if changed else "same",
            )

    return results
