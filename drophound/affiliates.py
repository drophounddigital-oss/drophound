"""Layer 3 — affiliate link building.

Outbound clicks route through `/go/{product_id}?to=<target>`, which appends the
configured affiliate tags and logs the click. This monetizes traffic whether or
not the visitor ever subscribes.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote, quote_plus, urlencode, urlparse, urlunparse, parse_qsl

from .config import Settings


def _field(row: Any, key: str) -> Any:
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return getattr(row, key, None)


def _add_params(url: str, params: dict[str, str]) -> str:
    parsed = urlparse(url)
    query = dict(parse_qsl(parsed.query))
    query.update(params)
    return urlunparse(parsed._replace(query=urlencode(query)))


def build_url(settings: Settings, product: Any, target: str) -> str:
    """Build the outbound URL for a product on a given marketplace/retailer.

    target ∈ {ebay, popmart, stockx}. eBay/StockX use a resale search seeded with
    the product name; popmart links straight to the product page.
    """
    target = (target or "ebay").lower()
    name = _field(product, "name") or _field(product, "character") or "blind box"
    brand = _field(product, "brand") or ""
    search_term = f"{brand} {name}".strip()

    if target == "popmart":
        retailer = (_field(product, "retailer") or "").lower()
        # Use search URLs — direct product pages go stale when items sell out or URLs change.
        if "pop mart" in brand.lower() or "popmart" in brand.lower():
            if "uk" in retailer:
                locale = "uk"
            elif "eu" in retailer or "de" in retailer:
                locale = "de"
            else:
                locale = "us"
            url = f"https://www.popmart.com/{locale}/search/{quote(name, safe='')}"
            if settings.popmart_affiliate_ref:
                return _add_params(url, {"ref": settings.popmart_affiliate_ref})
            return url
        if "smiski" in brand.lower():
            return f"https://www.smiski.com/search?q={quote_plus(name)}"
        if "sonny angel" in brand.lower():
            return f"https://www.sonnyangel-store.com/search?q={quote_plus(name)}"
        return f"https://www.google.com/search?q={quote_plus('buy ' + search_term)}"

    if target == "stockx":
        url = f"https://stockx.com/search?s={quote_plus(search_term)}"
        if settings.stockx_affiliate_ref:
            return _add_params(url, {"ref": settings.stockx_affiliate_ref})
        return url

    # Default: eBay sold/active search with EPN campaign tag.
    url = f"https://www.ebay.com/sch/i.html?_nkw={quote_plus(search_term)}"
    if settings.ebay_campaign_id:
        url = _add_params(url, {"mkcid": "1", "campid": settings.ebay_campaign_id})
    return url


TARGETS = ("ebay", "popmart", "stockx")
