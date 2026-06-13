from datetime import timedelta

from drophound.patterns import predict_restock, window_label
from drophound.util import iso, now_utc


def _events(days_ago_list, event_type="restock"):
    now = now_utc()
    return [{"event_type": event_type, "detected_at": iso(now - timedelta(days=d))}
            for d in days_ago_list]


def test_regular_cadence_predicts_high_confidence():
    # 5 restocks exactly 10 days apart, last one 2 days ago.
    events = _events([2, 12, 22, 32, 42])
    pred = predict_restock(events)
    assert pred["observations"] == 5
    assert abs(pred["cadence_days"] - 10) < 0.01
    assert pred["confidence"] == "high"
    # Next predicted ~8 days out (last + 10 day cadence).
    assert 7 < pred["days_until"] < 9
    assert pred["predicted_start"] < pred["predicted_next"] < pred["predicted_end"]


def test_insufficient_history_is_unknown():
    pred = predict_restock(_events([3]))
    assert pred["confidence"] == "unknown"
    assert pred["cadence_days"] is None
    assert window_label(pred) == "Not enough history yet"


def test_only_available_events_counted():
    # sold_out events must be ignored by the cadence model.
    events = _events([2, 12, 22], "restock") + _events([5, 15], "sold_out")
    pred = predict_restock(events)
    assert pred["observations"] == 3
