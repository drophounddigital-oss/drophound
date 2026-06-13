"""Resale price statistics.

Deliberately uses only the stdlib `statistics` module so the math is easy to
audit. Inputs are lists of completed-sale prices (floats).
"""

from __future__ import annotations

import statistics
from typing import Sequence


def resale_summary(prices: Sequence[float]) -> dict:
    """Summarize a set of sold prices into low/high/median/average/sample_size."""
    cleaned = [float(p) for p in prices if p is not None and float(p) > 0]
    if not cleaned:
        return {
            "sample_size": 0,
            "low": None,
            "high": None,
            "median": None,
            "average": None,
        }
    return {
        "sample_size": len(cleaned),
        "low": round(min(cleaned), 2),
        "high": round(max(cleaned), 2),
        "median": round(statistics.median(cleaned), 2),
        "average": round(statistics.fmean(cleaned), 2),
    }


def premium_multiple(retail_price: float | None, resale_median: float | None) -> float | None:
    """How many times retail an item resells for. 3.5 => 'sells for 3.5x retail'."""
    if not retail_price or not resale_median:
        return None
    if retail_price <= 0:
        return None
    return round(resale_median / retail_price, 2)


def trend_pct(previous: float | None, current: float | None) -> float | None:
    """Percentage change from previous to current. +12.5 means up 12.5%."""
    if previous in (None, 0) or current is None:
        return None
    return round((current - previous) / previous * 100, 1)
