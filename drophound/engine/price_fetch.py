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
import time
from urllib.parse import urlparse

import httpx

from .. import db
from ..config import Settings
from ..util import iso, now_utc

logger = logging.getLogger("drophound.price_fetch")

_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; DropHound/1.0; price-refresh)"}

# Domains that serve HTML from their .js endpoint (client-side frontends).
# Price scraping won't work for these without a headless browser.
_UNSUPPORTED_DOMAINS = ("popmart.com",)

# Status codes that mean "the store's WAF/rate-limiter pushed back", not "the
# product is gone" — must never be read as a dead link.
_BLOCKED_STATUS = {403, 429, 503}

# Minimum gap between requests to the same domain. A tight loop over hundreds
# of products from one store reads as scraping to most storefronts' WAFs and
# gets rate-limited mid-run — which, unthrottled, silently turns into a wave
# of false "dead link" results for a store that's actually fine.
_MIN_DOMAIN_GAP = 1.5


def _is_unsupported(url: str) -> bool:
    return any(d in url for d in _UNSUPPORTED_DOMAINS)


def _get_with_retry(
    client: httpx.Client, url: str, *, follow_redirects: bool = False, retries: int = 1
) -> httpx.Response | None:
    """GET with one retry (short backoff) on connection errors or a blocked status.

    Returns None if every attempt failed with an ambiguous error (timeout, odd
    status, etc) — the caller must treat that as "couldn't determine," not
    "confirmed dead". Re-raises httpx.ConnectError after retries are exhausted:
    unlike a timeout or a blocked status, "this host refuses to connect at
    all" (including DNS not resolving) is a real signal, not noise — callers
    that care may catch it specifically instead of getting it collapsed to None.
    """
    for attempt in range(retries + 1):
        try:
            r = client.get(url, timeout=15, follow_redirects=follow_redirects)
            if r.status_code in _BLOCKED_STATUS and attempt < retries:
                time.sleep(2.0)
                continue
            return r
        except httpx.ConnectError:
            if attempt < retries:
                time.sleep(2.0)
                continue
            raise
        except Exception as exc:
            if attempt < retries:
                time.sleep(2.0)
                continue
            logger.debug("request failed %s: %s", url, exc)
    return None


def _shopify_price(client: httpx.Client, product_url: str) -> tuple[float | None, bool | None]:
    """Return (price_dollars, available) from a Shopify product .js endpoint.

    The .js endpoint reports price in integer cents. Returns (None, None) if the
    endpoint is unavailable or returns HTML instead of JSON.
    """
    url = product_url.rstrip("/") + ".js"
    try:
        r = _get_with_retry(client, url)
    except httpx.ConnectError:
        r = None
    if r is None:
        return None, None
    ct = r.headers.get("content-type", "")
    if r.status_code != 200 or "json" not in ct:
        return None, None
    try:
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
        logger.debug("price parse failed %s: %s", url, exc)
        return None, None


def _looks_like_product_page(url: str) -> bool:
    """Heuristic: does this URL's path still look like a single product

    (not a redirect to a search/category/news/home page)?
    """
    path = urlparse(url).path.lower()
    return "/product" in path  # matches /products/... and /product/...


def check_url_ok(client: httpx.Client, product_url: str) -> bool | None:
    """Confirm a stored product_url still resolves to a real single-product page.

    Returns True (confirmed live), False (confirmed dead — safe to fall back to
    eBay), or None ("couldn't tell", e.g. rate-limited/blocked even after a
    retry). Callers must not treat None as False: an inconclusive check should
    leave the product's prior url_ok value untouched, not downgrade it.

    Two tiers: a Shopify store proves itself via its .js endpoint (real price
    data = real product). A client-rendered store (e.g. Pop Mart, whose .js
    endpoint serves HTML) is checked by following redirects and confirming both
    a 200 status and that the final URL still looks like a product page rather
    than a search/home/news redirect target. Only an unambiguous signal (a
    resolved product-shaped 200, an explicit 404/410, or the host refusing to
    connect at all on *both* tiers) counts as dead — anything else (blocked,
    timed out, odd status) is inconclusive.
    """
    js_url = product_url.rstrip("/") + ".js"
    js_connect_failed = False
    try:
        r = _get_with_retry(client, js_url)
    except httpx.ConnectError:
        r = None
        js_connect_failed = True

    if r is not None and r.status_code not in _BLOCKED_STATUS:
        if r.status_code == 200 and "json" in r.headers.get("content-type", ""):
            try:
                if (r.json().get("variants") or []):
                    return True
            except Exception:
                pass
        # else: not Shopify-JSON (client-rendered store) or no variants — fall
        # through to the plain-GET path check before concluding anything.

    try:
        r2 = _get_with_retry(client, product_url, follow_redirects=True)
    except httpx.ConnectError:
        # The .js host and the product-page host refused to connect on two
        # independent attempts (same domain either way) — the domain itself
        # is gone, not just a store rate-limiting us.
        return False if js_connect_failed else None

    if r2 is None or r2.status_code in _BLOCKED_STATUS:
        return None
    if r2.status_code == 200:
        return _looks_like_product_page(str(r2.url))
    if r2.status_code in (404, 410):
        return False
    return None


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
    last_hit: dict[str, float] = {}

    def _throttle(url: str) -> None:
        domain = urlparse(url).netloc
        gap = time.monotonic() - last_hit.get(domain, 0.0)
        if gap < _MIN_DOMAIN_GAP:
            time.sleep(_MIN_DOMAIN_GAP - gap)
        last_hit[domain] = time.monotonic()

    def _persist_url_ok(url_ok: bool | None, product_id: int) -> None:
        # None = inconclusive (e.g. rate-limited) — leave the prior value
        # alone rather than overwrite a good/unknown status with a guess.
        if url_ok is None or dry_run:
            return
        db.execute(
            conn,
            "UPDATE products SET url_ok = ?, url_checked_at = ? WHERE id = ?",
            (int(url_ok), iso(now_utc()), product_id),
        )

    with httpx.Client(
        follow_redirects=True, timeout=timeout, headers=_HEADERS
    ) as client:
        for p in products:
            url = p["product_url"]
            _throttle(url)

            if _is_unsupported(url):
                # Can't scrape a price (client-side JS store), but the Buy
                # link's validity is still checkable — drives the eBay
                # fallback in affiliates.build_url() independent of pricing.
                url_ok = check_url_ok(client, url)
                _persist_url_ok(url_ok, p["id"])
                results.append({
                    "name": p["name"],
                    "status": "unsupported",
                    "old_price": p["retail_price"],
                    "new_price": None,
                    "reason": "client-side JS store (update manually in catalog.json)",
                    "url_ok": url_ok,
                })
                continue

            price, available = _shopify_price(client, url)
            url_ok = True if price is not None else check_url_ok(client, url)
            _persist_url_ok(url_ok, p["id"])

            if price is None:
                results.append({
                    "name": p["name"],
                    "status": "fetch_failed",
                    "old_price": p["retail_price"],
                    "new_price": None,
                    "reason": "endpoint did not return JSON price data",
                    "url_ok": url_ok,
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
                "url_ok": url_ok,
            })
            logger.info(
                "%s  $%.2f → $%.2f  %s",
                p["name"], old or 0, price, "updated" if changed else "same",
            )

    return results
