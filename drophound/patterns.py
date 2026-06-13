"""Restock-pattern analysis.

Given a product's history of "became available" events (drops and restocks),
estimate the cadence and predict the next likely restock window. This powers
the premium "likely restock" feature from the plan.
"""

from __future__ import annotations

import statistics
from datetime import datetime, timedelta
from typing import Any, Sequence

from .util import now_utc, parse_iso

# Event types that mean "the item became buyable at this moment".
AVAILABLE_EVENTS = {"drop", "restock"}


def _field(row: Any, key: str) -> Any:
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return getattr(row, key, None)


def predict_restock(events: Sequence[Any], *, now: datetime | None = None) -> dict:
    """Estimate cadence + next restock window from a product's event history.

    `events` is any iterable of rows/dicts with `event_type` and `detected_at`
    (ISO string). Returns a dict with cadence_days, confidence, last_restock,
    and predicted_start/predicted_end (aware datetimes or None).
    """
    now = now or now_utc()
    times = sorted(
        parse_iso(_field(e, "detected_at"))
        for e in events
        if _field(e, "event_type") in AVAILABLE_EVENTS
    )

    result = {
        "observations": len(times),
        "cadence_days": None,
        "cadence_stdev": None,
        "confidence": "unknown",
        "last_restock": times[-1] if times else None,
        "predicted_next": None,
        "predicted_start": None,
        "predicted_end": None,
        "days_until": None,
    }

    if len(times) < 2:
        return result

    intervals = [
        (b - a).total_seconds() / 86400.0
        for a, b in zip(times, times[1:])
    ]
    avg = statistics.fmean(intervals)
    stdev = statistics.pstdev(intervals) if len(intervals) > 1 else 0.0
    if avg <= 0:
        return result

    last = times[-1]
    predicted_next = last + timedelta(days=avg)
    half_window = max(1.0, stdev / 2.0)
    predicted_start = predicted_next - timedelta(days=half_window)
    predicted_end = predicted_next + timedelta(days=half_window)

    cv = stdev / avg if avg else 1.0
    n = len(intervals)
    if n >= 4 and cv < 0.40:
        confidence = "high"
    elif n >= 3 and cv < 0.70:
        confidence = "medium"
    else:
        confidence = "low"

    result.update(
        cadence_days=round(avg, 1),
        cadence_stdev=round(stdev, 1),
        confidence=confidence,
        predicted_next=predicted_next,
        predicted_start=predicted_start,
        predicted_end=predicted_end,
        days_until=round((predicted_next - now).total_seconds() / 86400.0, 1),
    )
    return result


def window_label(prediction: dict) -> str:
    """Human-friendly summary of a prediction for UI/digests."""
    if prediction["confidence"] == "unknown" or not prediction["predicted_start"]:
        return "Not enough history yet"
    start = prediction["predicted_start"].strftime("%b %-d")
    end = prediction["predicted_end"].strftime("%b %-d")
    conf = prediction["confidence"]
    return f"~{start}–{end} ({conf} confidence)"
