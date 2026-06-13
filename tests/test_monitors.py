from drophound.cli import is_relevant
from drophound.config import get_settings
from drophound.engine.monitors import HttpMonitor, ShopifyStoreMonitor


class FakeResp:
    def __init__(self, json_data=None, text=""):
        self._json = json_data
        self.text = text

    def json(self):
        if self._json is None:
            raise ValueError("no json")
        return self._json


class FakeClient:
    """Returns canned responses keyed by exact URL."""
    def __init__(self, mapping):
        self.mapping = mapping

    def get(self, url):
        return self.mapping[url]


def _settings(monkeypatch):
    monkeypatch.setenv("DROPHOUND_HTTP_DELAY", "0")
    return get_settings()


def test_shopify_in_stock(monkeypatch):
    # The .js endpoint returns the product object directly with an `available` flag.
    url = "https://store.example.com/products/labubu"
    client = FakeClient({url + ".js": FakeResp(json_data={
        "available": True, "variants": [{"available": True}]})})
    mon = HttpMonitor(_settings(monkeypatch), client=client)
    obs = mon.check([{"id": 1, "product_url": url, "in_stock_signal": None, "retail_price": 15}])
    assert len(obs) == 1 and obs[0].status == "in_stock"


def test_shopify_sold_out(monkeypatch):
    url = "https://store.example.com/products/molly"
    client = FakeClient({url + ".js": FakeResp(json_data={
        "available": False, "variants": [{"available": False}]})})
    mon = HttpMonitor(_settings(monkeypatch), client=client)
    obs = mon.check([{"id": 2, "product_url": url, "in_stock_signal": None, "retail_price": 15}])
    assert obs[0].status == "sold_out"


def test_html_keyword_fallback(monkeypatch):
    # No /products/ in the URL -> not Shopify -> falls back to the HTML signal.
    url = "https://shop.example.com/p/123"
    client = FakeClient({url: FakeResp(text="<button>ADD TO CART</button>")})
    mon = HttpMonitor(_settings(monkeypatch), client=client)
    obs = mon.check([{"id": 3, "product_url": url, "in_stock_signal": "add to cart",
                      "retail_price": 10}])
    assert obs[0].status == "in_stock"


def test_js_shell_stays_silent(monkeypatch):
    # A JavaScript-rendered page with no signal and no sold-out marker (Pop Mart):
    # must produce NO observation rather than a false "sold out".
    url = "https://shop.example.com/p/js"
    client = FakeClient({url: FakeResp(text="<div id='__next'></div>")})
    mon = HttpMonitor(_settings(monkeypatch), client=client)
    obs = mon.check([{"id": 4, "product_url": url, "in_stock_signal": "add to bag",
                      "retail_price": 10}])
    assert obs == []


def test_shopify_store_monitor_batch(monkeypatch):
    domain = "store.example.com"
    page1 = {"products": [
        {"handle": "labubu-macaron", "variants": [{"available": True}]},
        {"handle": "molly-career", "variants": [{"available": False}]},
    ]}
    client = FakeClient({
        f"https://{domain}/products.json?limit=250&page=1": FakeResp(json_data=page1),
    })
    mon = ShopifyStoreMonitor(_settings(monkeypatch), client=client)
    products = [
        {"id": 10, "product_url": f"https://{domain}/products/labubu-macaron", "retail_price": 15},
        {"id": 11, "product_url": f"https://{domain}/products/molly-career", "retail_price": 15},
        {"id": 12, "product_url": f"https://{domain}/products/not-in-store", "retail_price": 15},
    ]
    statuses = {o.product_id: o.status for o in mon.check(products)}
    assert statuses == {10: "in_stock", 11: "sold_out"}  # #12 absent -> no observation


def test_is_relevant_filter():
    kws = ["blind box", "labubu", "smiski"]
    assert is_relevant("Labubu Exciting Macaron", "Pop Mart", "Blind Box", kws)
    assert is_relevant("Random Series", "BrandX", "Blind Box", kws)
    assert not is_relevant("Funko Pop Batman", "Funko", "Vinyl", kws)
