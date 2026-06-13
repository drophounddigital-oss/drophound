import json

from drophound import db
from drophound.config import FIXTURES_DIR
from drophound.engine import resale
from drophound.stats import resale_summary


def test_refresh_product_uses_fixtures_offline(conn, settings):
    product = db.one(conn, "SELECT * FROM products WHERE sku = ?", ("PM-LAB-MAC-01",))
    fixtures = json.loads((FIXTURES_DIR / "ebay_sold.json").read_text())
    expected = resale_summary(fixtures["PM-LAB-MAC-01"])

    before = db.one(conn, "SELECT COUNT(*) c FROM resale_prices WHERE product_id=?",
                    (product["id"],))["c"]
    summary = resale.refresh_product(conn, settings, product)
    after = db.one(conn, "SELECT COUNT(*) c FROM resale_prices WHERE product_id=?",
                   (product["id"],))["c"]

    assert summary["source"] == "fixture"
    assert summary["sample_size"] == expected["sample_size"]
    assert summary["median"] == expected["median"]
    assert summary["low"] == expected["low"]
    assert summary["high"] == expected["high"]
    assert after == before + 1


def test_latest_and_previous(conn):
    product = db.one(conn, "SELECT * FROM products LIMIT 1")
    latest = resale.latest(conn, product["id"])
    previous = resale.previous(conn, product["id"])
    # Seed inserts two snapshots (older + current); current median >= older.
    assert latest is not None and previous is not None
    assert latest["median"] >= previous["median"]
