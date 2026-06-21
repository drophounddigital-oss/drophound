"""Layer 3 — affiliate link building.

Outbound clicks route through `/go/{product_id}?to=<target>`, which appends the
configured affiliate tags and logs the click.

Targets:
  site    → product's own retailer page (stored product_url), character search fallback
  ebay    → eBay sold/active search (resale discovery)
  stockx  → StockX search (resale discovery)
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


def _retailer_search_url(product: Any) -> str:
    """Brand-aware search fallback using character name (not full product name)."""
    brand = (_field(product, "brand") or "").lower()
    retailer = (_field(product, "retailer") or "").lower()
    # Character name is short and specific; full product name returns 100+ results.
    character = _field(product, "character") or _field(product, "name") or "blind box"

    if "pop mart" in brand or "popmart" in brand:
        if "uk" in retailer:
            locale = "uk"
        elif "eu" in retailer or "de" in retailer:
            locale = "de"
        else:
            locale = "us"
        return f"https://www.popmart.com/{locale}/search/{quote(character, safe='')}"

    if "smiski" in brand:
        return f"https://www.smiski.com/search?q={quote_plus(character)}"

    if "sonny angel" in brand or "sonnyangel" in brand:
        return f"https://www.sonnyangel-store.com/search?q={quote_plus(character)}"

    name = _field(product, "name") or character
    search_term = ((_field(product, "brand") or "") + " " + name).strip()
    return f"https://www.google.com/search?q={quote_plus('buy ' + search_term)}"


def build_url(settings: Settings, product: Any, target: str) -> str:
    """Build the outbound URL for a product on a given marketplace/retailer.

    target ∈ {site, ebay, stockx}.

    'site' (default) sends the user to the product's own retailer page via the
    stored product_url. If no URL is stored, falls back to a character-name search
    on the appropriate retailer site.
    """
    target = (target or "site").lower()

    if target in ("site", "popmart", "smiski", "sonnyangel"):
        brand = (_field(product, "brand") or "").lower()
        retailer = (_field(product, "retailer") or "").lower()

        # Pop Mart: route to eBay search. Their own product slugs are internal
        # IDs that throw client-side errors, and their search returns their own
        # catalog only. eBay has broad reseller listings for every Pop Mart SKU.
        if "pop mart" in brand or "popmart" in brand:
            name = _field(product, "name") or _field(product, "character") or "blind box"
            url = f"https://www.ebay.com/sch/i.html?_nkw={quote_plus(('Pop Mart ' + name).strip())}"
            if settings.ebay_campaign_id:
                url = _add_params(url, {"mkcid": "1", "campid": settings.ebay_campaign_id})
            return url

        # All other brands: use the stored product URL (Amazon, etc.).
        url = (_field(product, "product_url") or "").strip()
        if url and url.startswith("http"):
            return url
        # No stored URL — fall back to character-name search on the right site.
        return _retailer_search_url(product)

    if target == "stockx":
        name = _field(product, "name") or _field(product, "character") or "blind box"
        brand = _field(product, "brand") or ""
        url = f"https://stockx.com/search?s={quote_plus((brand + ' ' + name).strip())}"
        if settings.stockx_affiliate_ref:
            return _add_params(url, {"ref": settings.stockx_affiliate_ref})
        return url

    # Default: eBay search (resale).
    name = _field(product, "name") or _field(product, "character") or "blind box"
    brand = _field(product, "brand") or ""
    url = f"https://www.ebay.com/sch/i.html?_nkw={quote_plus((brand + ' ' + name).strip())}"
    if settings.ebay_campaign_id:
        url = _add_params(url, {"mkcid": "1", "campid": settings.ebay_campaign_id})
    return url


TARGETS = ("site", "ebay", "stockx")
