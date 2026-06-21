from drophound.affiliates import build_url
from drophound.config import get_settings

POPMART_US = {
    "brand": "Pop Mart",
    "name": "Labubu Exciting Macaron Blind Box",
    "character": "Labubu",
    "retailer": "Pop Mart US",
    "product_url": "https://www.popmart.com/us/products/labubu-exciting-macaron",
}

POPMART_EU = {
    "brand": "Pop Mart",
    "name": "Labubu Let's Checkmate Blind Box",
    "character": "Labubu",
    "retailer": "Pop Mart EU",
    "product_url": "https://www.popmart.com/de/products/labubu-lets-checkmate",
}

POPMART_UK = {
    "brand": "Pop Mart",
    "name": "Skullpanda Image of Reality Blind Box",
    "character": "Skullpanda",
    "retailer": "Pop Mart UK",
    "product_url": "https://www.popmart.com/uk/products/skullpanda-image-of-reality",
}

SMISKI = {
    "brand": "Smiski",
    "name": "Smiski Living Series",
    "character": "Smiski",
    "retailer": "Smiski US",
    "product_url": "https://www.amazon.com/s?k=Smiski+Living+Series",
}

SONNY_ANGEL = {
    "brand": "Sonny Angel",
    "name": "Sonny Angel Hippers Blind Box",
    "character": "Sonny Angel",
    "retailer": "Sonny Angel US",
    "product_url": "https://www.amazon.com/s?k=Sonny+Angel+Hippers",
}


# --- Pop Mart: routes to eBay (own site slugs throw strconv errors) --------

def test_popmart_site_routes_to_ebay():
    settings = get_settings()
    url = build_url(settings, POPMART_US, "site")
    assert url.startswith("https://www.ebay.com/sch/")
    assert "Pop+Mart" in url
    assert "Labubu" in url


def test_popmart_eu_also_routes_to_ebay():
    settings = get_settings()
    url = build_url(settings, POPMART_EU, "site")
    assert url.startswith("https://www.ebay.com/sch/")


def test_popmart_uk_also_routes_to_ebay():
    settings = get_settings()
    url = build_url(settings, POPMART_UK, "site")
    assert url.startswith("https://www.ebay.com/sch/")


# --- Non-Pop Mart brands: use stored product_url directly ------------------

def test_smiski_uses_stored_url():
    settings = get_settings()
    assert build_url(settings, SMISKI, "site") == "https://www.amazon.com/s?k=Smiski+Living+Series"


def test_sonny_angel_uses_stored_url():
    settings = get_settings()
    assert build_url(settings, SONNY_ANGEL, "site") == "https://www.amazon.com/s?k=Sonny+Angel+Hippers"


# --- eBay target ----------------------------------------------------------

def test_ebay_search_url_without_campaign(monkeypatch):
    monkeypatch.delenv("EBAY_CAMPAIGN_ID", raising=False)
    settings = get_settings()
    url = build_url(settings, POPMART_US, "ebay")
    assert url.startswith("https://www.ebay.com/sch/")
    assert "Labubu" in url
    assert "campid" not in url


def test_ebay_url_includes_campaign_when_set(monkeypatch):
    monkeypatch.setenv("EBAY_CAMPAIGN_ID", "5338999999")
    settings = get_settings()
    url = build_url(settings, POPMART_US, "ebay")
    assert "campid=5338999999" in url


# --- stockx target --------------------------------------------------------

def test_stockx_target_is_search():
    settings = get_settings()
    url = build_url(settings, POPMART_US, "stockx")
    assert url.startswith("https://stockx.com/search")
