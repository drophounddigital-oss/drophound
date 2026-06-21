from drophound.affiliates import build_url
from drophound.config import get_settings

PRODUCT = {
    "brand": "Pop Mart",
    "name": "Labubu Exciting Macaron Blind Box",
    "character": "Labubu",
    "retailer": "toytokyo.com",  # a real store domain
    "product_url": "https://toytokyo.com/products/pop-mart-labubu-exciting-macaron",
}

# A product with no stored URL — should fall back to a search.
DEMO_PRODUCT = {
    "brand": "Pop Mart",
    "name": "Labubu Big Into Energy",
    "character": "Labubu",
    "retailer": "Pop Mart US",
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


def test_popmart_real_store_uses_product_url():
    settings = get_settings()
    url = build_url(settings, PRODUCT, "popmart")
    assert url.startswith("https://toytokyo.com/products/pop-mart-labubu-exciting-macaron")


def test_popmart_demo_falls_back_to_search():
    # Placeholder URL -> a working Pop Mart search, never a dead product page.
    settings = get_settings()
    url = build_url(settings, DEMO_PRODUCT, "popmart")
    assert url.startswith("https://www.popmart.com/us/search/")
    assert "Labubu" in url


def test_stockx_target_is_search():
    settings = get_settings()
    url = build_url(settings, PRODUCT, "stockx")
    assert url.startswith("https://stockx.com/search")
