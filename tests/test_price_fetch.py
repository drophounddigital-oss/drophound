import httpx

from drophound.engine import price_fetch
from drophound.engine.price_fetch import check_url_ok


class _FakeResponse:
    def __init__(self, status_code=200, url="", content_type=None, json_data=None):
        self.status_code = status_code
        self.url = url
        self.headers = {"content-type": content_type} if content_type else {}
        self._json_data = json_data

    def json(self):
        return self._json_data


class _FakeClient:
    """Serves canned responses keyed by exact URL. A value can be a single
    _FakeResponse (returned every call) or a list, consumed one per call —
    used to simulate "blocked, then succeeds on retry"."""

    def __init__(self, responses: dict):
        self._responses = {k: (v if isinstance(v, list) else [v]) for k, v in responses.items()}

    def get(self, url, timeout=None, follow_redirects=None):
        if url not in self._responses:
            raise RuntimeError(f"unexpected request: {url}")
        queue = self._responses[url]
        return queue.pop(0) if len(queue) > 1 else queue[0]


def test_shopify_product_with_variants_is_ok():
    url = "https://www.smiski.com/products/living-series"
    client = _FakeClient({
        url + ".js": _FakeResponse(
            status_code=200, content_type="application/json",
            json_data={"available": True, "variants": [{"price": "1999", "available": True}]},
        ),
    })
    assert check_url_ok(client, url) is True


def test_dead_shopify_redirect_falls_through_to_path_check():
    url = "https://www.smiski.com/products/living-series"
    client = _FakeClient({
        url + ".js": _FakeResponse(status_code=404, content_type="text/html"),
        url: _FakeResponse(status_code=404, url="https://smiski.com/news/living-series/"),
    })
    assert check_url_ok(client, url) is False


def test_client_rendered_store_confirmed_by_product_path():
    url = "https://www.popmart.com/us/products/labubu-exciting-macaron"
    client = _FakeClient({
        url + ".js": _FakeResponse(status_code=200, content_type="text/html"),
        url: _FakeResponse(status_code=200, url=url),
    })
    assert check_url_ok(client, url) is True


def test_client_rendered_store_redirected_to_search_is_dead():
    url = "https://www.popmart.com/us/products/discontinued-item"
    client = _FakeClient({
        url + ".js": _FakeResponse(status_code=200, content_type="text/html"),
        url: _FakeResponse(status_code=200, url="https://www.popmart.com/us/search"),
    })
    assert check_url_ok(client, url) is False


# --- The core regression this suite guards against: a WAF/rate-limit block --
# must never be read as "the product is gone". Seeing that happen in
# production (hundreds of live products on one store all flagged dead after
# a burst of requests tripped its rate limiter) is exactly why check_url_ok
# returns a tri-state instead of a plain bool.

def test_blocked_status_is_inconclusive_not_dead(monkeypatch):
    monkeypatch.setattr(price_fetch.time, "sleep", lambda *_: None)
    url = "https://strangecattoys.com/products/umashika-figure-collection"
    client = _FakeClient({
        # Every attempt (initial + retry) comes back rate-limited.
        url + ".js": [_FakeResponse(status_code=429), _FakeResponse(status_code=429)],
        url: [_FakeResponse(status_code=429), _FakeResponse(status_code=429)],
    })
    assert check_url_ok(client, url) is None


def test_network_exception_is_inconclusive_not_dead(monkeypatch):
    monkeypatch.setattr(price_fetch.time, "sleep", lambda *_: None)
    url = "https://www.sonnyangel-store.com/products/animal-series-4"

    class _RaisingClient:
        def get(self, *a, **k):
            raise RuntimeError("Could not resolve host")

    assert check_url_ok(_RaisingClient(), url) is None


def test_domain_that_wont_connect_at_all_is_confirmed_dead(monkeypatch):
    # Unlike a rate-limit block or a generic error, a host that refuses to
    # connect (DNS doesn't resolve, connection refused) on both independent
    # tiers is a real "this domain is gone" signal, not noise — e.g. a
    # storefront that shut down entirely, distinct from one that's just
    # temporarily rate-limiting us.
    monkeypatch.setattr(price_fetch.time, "sleep", lambda *_: None)
    url = "https://www.sonnyangel-store.com/products/animal-series-4"

    class _UnresolvableClient:
        def get(self, *a, **k):
            raise httpx.ConnectError("Could not resolve host")

    assert check_url_ok(_UnresolvableClient(), url) is False


def test_transient_block_recovers_on_retry(monkeypatch):
    monkeypatch.setattr(price_fetch.time, "sleep", lambda *_: None)
    url = "https://strangecattoys.com/products/umashika-figure-collection"
    client = _FakeClient({
        # Rate-limited on the first attempt, succeeds on the retry.
        url + ".js": [
            _FakeResponse(status_code=429),
            _FakeResponse(
                status_code=200, content_type="application/json",
                json_data={"available": True, "variants": [{"price": "1999", "available": True}]},
            ),
        ],
    })
    assert check_url_ok(client, url) is True
