"""Subscriber alert filters (the premium 'filter by character/region/price' feature).

A free subscriber gets the unfiltered broadcast. A premium subscriber can narrow
alerts by brand, character, region, and a price ceiling. An empty filter on a
dimension means "match everything on that dimension".
"""

from __future__ import annotations

from typing import Any


def _csv(value: str | None) -> list[str]:
    if not value:
        return []
    return [v.strip().lower() for v in value.split(",") if v.strip()]


def _field(row: Any, key: str) -> Any:
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return getattr(row, key, None)


def matches(subscriber: Any, product: Any, *, price: float | None = None) -> bool:
    """Return True if this product/price should alert this subscriber.

    `price` is the event price if known; falls back to the product retail price.
    """
    brands = _csv(_field(subscriber, "filter_brands"))
    characters = _csv(_field(subscriber, "filter_characters"))
    regions = _csv(_field(subscriber, "filter_regions"))
    ceiling = _field(subscriber, "price_ceiling")

    if brands and (_field(product, "brand") or "").lower() not in brands:
        return False
    if characters and (_field(product, "character") or "").lower() not in characters:
        return False
    if regions and (_field(product, "region") or "").lower() not in regions:
        return False

    if ceiling is not None:
        effective_price = price if price is not None else _field(product, "retail_price")
        if effective_price is not None and float(effective_price) > float(ceiling):
            return False

    return True


def describe(subscriber: Any) -> str:
    """Render a subscriber's filters as a readable string for the dashboard."""
    parts = []
    for label, key in (
        ("brands", "filter_brands"),
        ("characters", "filter_characters"),
        ("regions", "filter_regions"),
    ):
        vals = _csv(_field(subscriber, key))
        if vals:
            shown = [v.upper() if key == "filter_regions" else v.title() for v in vals]
            parts.append(f"{label}: {', '.join(shown)}")
    ceiling = _field(subscriber, "price_ceiling")
    if ceiling is not None:
        parts.append(f"max ${float(ceiling):,.0f}")
    return " · ".join(parts) if parts else "Everything (no filters set)"
