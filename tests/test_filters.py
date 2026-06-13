from drophound.filters import describe, matches

SUB = {
    "filter_brands": "Pop Mart",
    "filter_characters": "Labubu,Molly",
    "filter_regions": "US",
    "price_ceiling": 50,
}
LABUBU_US = {"brand": "Pop Mart", "character": "Labubu", "region": "US", "retail_price": 16}


def test_match_all_dimensions():
    assert matches(SUB, LABUBU_US, price=16) is True


def test_character_filter_excludes():
    other = {**LABUBU_US, "character": "Skullpanda"}
    assert matches(SUB, other) is False


def test_region_filter_excludes():
    uk = {**LABUBU_US, "region": "UK"}
    assert matches(SUB, uk) is False


def test_price_ceiling_excludes():
    assert matches(SUB, LABUBU_US, price=120) is False


def test_empty_filters_match_everything():
    empty = {"filter_brands": "", "filter_characters": "", "filter_regions": "",
             "price_ceiling": None}
    assert matches(empty, LABUBU_US) is True
    assert describe(empty).startswith("Everything")


def test_describe_lists_filters():
    text = describe(SUB)
    assert "Labubu" in text and "US" in text and "50" in text
