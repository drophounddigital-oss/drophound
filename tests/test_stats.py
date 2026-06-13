from drophound.stats import premium_multiple, resale_summary, trend_pct


def test_resale_summary_basic():
    s = resale_summary([10, 20, 30])
    assert s["sample_size"] == 3
    assert s["low"] == 10
    assert s["high"] == 30
    assert s["median"] == 20
    assert s["average"] == 20


def test_resale_summary_ignores_nonpositive_and_none():
    s = resale_summary([0, -5, None, 40, 20])
    assert s["sample_size"] == 2
    assert s["low"] == 20
    assert s["high"] == 40


def test_resale_summary_empty():
    s = resale_summary([])
    assert s["sample_size"] == 0
    assert s["median"] is None


def test_premium_multiple():
    assert premium_multiple(10, 35) == 3.5
    assert premium_multiple(0, 35) is None
    assert premium_multiple(None, 35) is None
    assert premium_multiple(10, None) is None


def test_trend_pct():
    assert trend_pct(100, 110) == 10.0
    assert trend_pct(100, 80) == -20.0
    assert trend_pct(0, 5) is None
    assert trend_pct(None, 5) is None
