from drophound.affiliates import build_url
from drophound.config import get_settings

PRODUCT = {
    "brand": "Pop Mart",
    "name": "Labubu Exciting Macaron Blind Box",
    "character": "Labubu",
    "retailer": "Pop Mart US",
    "product_url": "https://www.popmart.com/us/products/labubu-exciting-macaron",
}

PRODUCT_EU = {
    "brand": "Pop Mart",
    "name": "Labubu Let's Checkmate Blind Box",
    "character": "Labubu",
    "retailer": "Pop Mart EU",
    "product_url": "https://www.popmart.com/de/products/labubu-lets-checkmate",
}

PRODUCT_UK = {
    "brand": "Pop Mart",
    "name": "Skullpanda Image of Reality",
    "character": "Skullpanda",
    "retailer": "Pop Mart UK",
    "product_url": "",
}


def test_ebay_search_url_without_campaign(monkeypatch):
    monkeypatch.delenv("EBAY_CAMPAIGN_ID", raising=False)
    settings = get_settings()
    url = build_url(settings, PRODUCT, "ebay")
    assert url.startswith("https://www.ebay.com/sch/")
    assert "Labubu" in url or "Labubu".lower() in url.lower()
    assert "campid" not in url


def test_ebay_url_includes_campaign_when_set(monkeypatch):
    monkeypatch.setenv("EBAY_CAMPAIGN_ID", "5338999999")
    settings = get_settings()
    url = build_url(settings, PRODUCT, "ebay")
    assert "campid=5338999999" in url


def test_popmart_us_uses_search():
    # Always use search — direct product URLs go 404 when items sell out.
    settings = get_settings()
    url = build_url(settings, PRODUCT, "popmart")
    assert url.startswith("https://www.popmart.com/us/search/")
    assert "Labubu" in url


def test_popmart_eu_uses_de_locale():
    settings = get_settings()
    url = build_url(settings, PRODUCT_EU, "popmart")
    assert url.startswith("https://www.popmart.com/de/search/")


def test_popmart_uk_uses_uk_locale():
    settings = get_settings()
    url = build_url(settings, PRODUCT_UK, "popmart")
    assert url.startswith("https://www.popmart.com/uk/search/")


def test_stockx_target_is_search():
    settings = get_settings()
    url = build_url(settings, PRODUCT, "stockx")
    assert url.startswith("https://stockx.com/search")
