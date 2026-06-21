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

POPMART_NO_URL = {
    "brand": "Pop Mart",
    "name": "Labubu Unknown Series",
    "character": "Labubu",
    "retailer": "Pop Mart US",
    "product_url": "",
}

SMISKI = {
    "brand": "Smiski",
    "name": "Smiski Living Series",
    "character": "Smiski",
    "retailer": "Smiski US",
    "product_url": "https://www.smiski.com/products/living-series",
}

SONNY_ANGEL = {
    "brand": "Sonny Angel",
    "name": "Sonny Angel Hippers Blind Box",
    "character": "Sonny Angel",
    "retailer": "Sonny Angel US",
    "product_url": "https://www.sonnyangel-store.com/products/hippers",
}


# --- site target (direct product page) ------------------------------------

def test_site_target_uses_stored_url():
    settings = get_settings()
    url = build_url(settings, POPMART_US, "site")
    assert url == "https://www.popmart.com/us/products/labubu-exciting-macaron"


def test_site_target_smiski_uses_stored_url():
    settings = get_settings()
    assert build_url(settings, SMISKI, "site") == "https://www.smiski.com/products/living-series"


def test_site_target_sonny_angel_uses_stored_url():
    settings = get_settings()
    assert build_url(settings, SONNY_ANGEL, "site") == "https://www.sonnyangel-store.com/products/hippers"


def test_site_target_falls_back_to_character_search_when_no_url():
    settings = get_settings()
    url = build_url(settings, POPMART_NO_URL, "site")
    # Fallback: character-name search (not full product name) — short and focused
    assert url.startswith("https://www.popmart.com/us/search/")
    assert "Labubu" in url
    assert "+" not in url  # must use %20, not + (path segment)


def test_popmart_eu_fallback_uses_de_locale():
    no_url = {**POPMART_EU, "product_url": ""}
    settings = get_settings()
    assert build_url(settings, no_url, "site").startswith("https://www.popmart.com/de/search/")


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
