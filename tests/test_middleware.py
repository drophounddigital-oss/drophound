"""Tests for rate limiting, session cookies, caching, validation, and error pages."""

import time

from drophound import cache
from drophound.validation import (
    make_token, validate_email, sanitize_str, verify_token,
)


# ---- cache ---------------------------------------------------------------- #
def test_cache_set_get():
    cache.set("test:k1", {"x": 1}, ttl=5)
    assert cache.get("test:k1") == {"x": 1}


def test_cache_expiry():
    cache.set("test:exp", "hi", ttl=1)
    time.sleep(1.1)
    assert cache.get("test:exp") is None


def test_cache_invalidate():
    cache.set("test:a", 1); cache.set("test:b", 2); cache.set("other:c", 3)
    cache.invalidate("test:")
    assert cache.get("test:a") is None
    assert cache.get("test:b") is None
    assert cache.get("other:c") == 3   # different prefix, untouched


# ---- validation ----------------------------------------------------------- #
def test_validate_email_ok():
    assert validate_email("  User@Example.COM  ") == "user@example.com"


def test_validate_email_bad():
    import pytest
    with pytest.raises(ValueError):
        validate_email("notanemail")
    with pytest.raises(ValueError):
        validate_email("")


def test_sanitize_strips_html():
    assert "<" not in sanitize_str("<script>alert(1)</script>")
    assert "'" not in sanitize_str("'; DROP TABLE")


def test_sanitize_truncates():
    assert len(sanitize_str("x" * 500, max_len=10)) == 10


# ---- expiring tokens ------------------------------------------------------ #
def test_token_roundtrip():
    tok = make_token("reset:42", "secret", ttl_seconds=60)
    assert verify_token(tok, "secret") == "reset:42"


def test_token_wrong_secret():
    tok = make_token("reset:42", "secret")
    assert verify_token(tok, "wrong") is None


def test_token_expired():
    tok = make_token("reset:42", "secret", ttl_seconds=0)
    time.sleep(0.01)
    assert verify_token(tok, "secret") is None


# ---- rate limiting (via test client) -------------------------------------- #
def test_rate_limit_global(client):
    # /api/health is not rate-limited at the endpoint level, so 5 quick
    # hits should all pass fine (global limit is 300/min, way above 5).
    for _ in range(5):
        r = client.get("/api/health")
        assert r.status_code == 200


# ---- error pages ---------------------------------------------------------- #
def test_404_returns_html(client):
    r = client.get("/this-page-does-not-exist-at-all")
    assert r.status_code == 404
    assert "404" in r.text


def test_404_api_returns_json(client):
    r = client.get("/api/nonexistent-endpoint")
    assert r.status_code == 404
    assert r.json()["error"]


# ---- session cookie ------------------------------------------------------- #
def test_session_cookie_set(client):
    r = client.get("/")
    # Session middleware sets dh_sid cookie on first visit
    assert "dh_sid" in r.cookies or "dh_sid" in r.headers.get("set-cookie", "")


# ---- watchlist auth + input validation ------------------------------------ #
def test_watch_add_requires_login(client):
    # No session → 401
    r = client.post("/watch/add", data={"product_id": "1"})
    assert r.status_code == 401
    assert "error" in r.json()


def test_watch_add_bad_pid(client):
    # Bad product_id → 400 even before session check (pid validated first)
    r = client.post("/watch/add", data={"product_id": "abc"})
    assert r.status_code == 400


def test_watch_remove_requires_login(client):
    r = client.post("/watch/remove", data={"product_id": "1"})
    assert r.status_code == 401
