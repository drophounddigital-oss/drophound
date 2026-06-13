from drophound.affiliates import build_url
from drophound.config import get_settings

PRODUCT = {
    "brand": "Pop Mart",
    "name": "Labubu Exciting Macaron Blind Box",
    "character": "Labubu",
    "product_url": "https://www.popmart.com/us/products/labubu-exciting-macaron",
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


def test_popmart_target_uses_product_url():
    settings = get_settings()
    url = build_url(settings, PRODUCT, "popmart")
    assert url.startswith("https://www.popmart.com/us/products/labubu-exciting-macaron")


def test_stockx_target_is_search():
    settings = get_settings()
    url = build_url(settings, PRODUCT, "stockx")
    assert url.startswith("https://stockx.com/search")
