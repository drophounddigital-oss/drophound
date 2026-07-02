from drophound.affiliates import build_url
from drophound.config import get_settings

POPMART_CONFIRMED_OK = {
    "brand": "Pop Mart",
    "name": "Labubu Exciting Macaron Blind Box",
    "character": "Labubu",
    "retailer": "Pop Mart US",
    "product_url": "https://www.popmart.com/us/products/labubu-exciting-macaron",
    "url_ok": 1,
}

POPMART_NEVER_CHECKED = {
    "brand": "Pop Mart",
    "name": "Labubu Let's Checkmate Blind Box",
    "character": "Labubu",
    "retailer": "Pop Mart EU",
    "product_url": "https://www.popmart.com/de/products/labubu-lets-checkmate",
    "url_ok": None,
}

POPMART_CONFIRMED_DEAD = {
    "brand": "Pop Mart",
    "name": "Skullpanda Image of Reality Blind Box",
    "character": "Skullpanda",
    "retailer": "Pop Mart UK",
    "product_url": "https://www.popmart.com/uk/products/skullpanda-image-of-reality",
    "url_ok": 0,
}

SMISKI_CONFIRMED_DEAD = {
    "brand": "Smiski",
    "name": "Smiski Living Series",
    "character": "Smiski",
    "retailer": "Smiski US",
    "product_url": "https://www.smiski.com/products/living-series",
    "url_ok": 0,
}

SONNY_ANGEL_CONFIRMED_OK = {
    "brand": "Sonny Angel",
    "name": "Sonny Angel Hippers Blind Box",
    "character": "Sonny Angel",
    "retailer": "Sonny Angel US",
    "product_url": "https://www.sonnyangel-store.com/products/hippers",
    "url_ok": 1,
}


# --- site target: direct link when not confirmed dead ----------------------

def test_confirmed_ok_url_goes_direct_to_retailer():
    settings = get_settings()
    url = build_url(settings, POPMART_CONFIRMED_OK, "site")
    assert url == POPMART_CONFIRMED_OK["product_url"]


def test_never_checked_url_is_tried_directly():
    settings = get_settings()
    url = build_url(settings, POPMART_NEVER_CHECKED, "site")
    assert url == POPMART_NEVER_CHECKED["product_url"]


def test_confirmed_ok_applies_regardless_of_brand():
    settings = get_settings()
    url = build_url(settings, SONNY_ANGEL_CONFIRMED_OK, "site")
    assert url == SONNY_ANGEL_CONFIRMED_OK["product_url"]


# --- site target: eBay fallback only once confirmed dead --------------------

def test_confirmed_dead_url_falls_back_to_ebay():
    settings = get_settings()
    url = build_url(settings, POPMART_CONFIRMED_DEAD, "site")
    assert url.startswith("https://www.ebay.com/sch/")
    assert "Skullpanda" in url


def test_confirmed_dead_smiski_falls_back_to_ebay_not_amazon():
    settings = get_settings()
    url = build_url(settings, SMISKI_CONFIRMED_DEAD, "site")
    assert url.startswith("https://www.ebay.com/sch/")
    assert "amazon" not in url.lower()


def test_missing_product_url_falls_back_to_ebay():
    settings = get_settings()
    product = {"brand": "Smiski", "name": "Smiski Bath Series", "character": "Smiski",
               "product_url": None, "url_ok": None}
    url = build_url(settings, product, "site")
    assert url.startswith("https://www.ebay.com/sch/")


# --- eBay target ----------------------------------------------------------

def test_ebay_search_url_without_campaign(monkeypatch):
    monkeypatch.delenv("EBAY_CAMPAIGN_ID", raising=False)
    settings = get_settings()
    url = build_url(settings, POPMART_CONFIRMED_OK, "ebay")
    assert url.startswith("https://www.ebay.com/sch/")
    assert "Labubu" in url
    assert "campid" not in url


def test_ebay_url_includes_campaign_when_set(monkeypatch):
    monkeypatch.setenv("EBAY_CAMPAIGN_ID", "5338999999")
    settings = get_settings()
    url = build_url(settings, POPMART_CONFIRMED_OK, "ebay")
    assert "campid=5338999999" in url


# --- stockx target --------------------------------------------------------

def test_stockx_target_is_search():
    settings = get_settings()
    url = build_url(settings, POPMART_CONFIRMED_OK, "stockx")
    assert url.startswith("https://stockx.com/search")
