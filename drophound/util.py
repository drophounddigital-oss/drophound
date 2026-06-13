"""Small, dependency-free helpers shared across the package."""

from __future__ import annotations

from datetime import datetime, timezone


def now_utc() -> datetime:
    """Timezone-aware 'now' in UTC (utcnow() is deprecated in 3.12+)."""
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    """Serialize a datetime to an ISO-8601 string (UTC, +00:00 offset)."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def parse_iso(value: str) -> datetime:
    """Parse an ISO-8601 string back into an aware UTC datetime."""
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def humanize_age(dt: datetime, *, now: datetime | None = None) -> str:
    """'3m ago', '2h ago', '4d ago' — compact relative time for the feed."""
    now = now or now_utc()
    seconds = max(0, int((now - dt).total_seconds()))
    if seconds < 60:
        return "just now"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h ago"
    days = hours // 24
    return f"{days}d ago"


def money(amount: float | None, currency: str = "USD") -> str:
    """Format a price for display. None -> em dash."""
    if amount is None:
        return "—"
    symbol = {"USD": "$", "GBP": "£", "EUR": "€"}.get(currency, "")
    return f"{symbol}{amount:,.2f}"


_THUMB_PALETTE = [
    "#f4a6c0", "#ff9aa2", "#ffb38a", "#ffd166", "#b5e48c", "#90e0ef",
    "#8ec5ff", "#a0b4ff", "#b39ddb", "#d4a5e8", "#95d5b2", "#74c69d",
]


def thumb_color(seed: str | None) -> str:
    """Deterministic, pleasant thumbnail color derived from a product string."""
    if not seed:
        return _THUMB_PALETTE[0]
    h = 0
    for ch in seed:
        h = (h * 31 + ord(ch)) & 0xFFFFFFFF
    return _THUMB_PALETTE[h % len(_THUMB_PALETTE)]


def slugify(value: str) -> str:
    out = []
    for ch in value.lower().strip():
        if ch.isalnum():
            out.append(ch)
        elif ch in " -_/":
            out.append("-")
    slug = "".join(out)
    while "--" in slug:
        slug = slug.replace("--", "-")
    return slug.strip("-")
