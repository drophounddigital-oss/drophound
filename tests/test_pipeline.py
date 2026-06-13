from drophound import db
from drophound.engine.monitors import Observation
from drophound.engine.pipeline import run_cycle


class StubMonitor:
    """Emits a single, fixed restock observation for a chosen product."""
    name = "stub"

    def __init__(self, product_id, price):
        self.product_id = product_id
        self.price = price

    def check(self, products):
        return [Observation(self.product_id, "in_stock", self.price,
                            "restock", "Back in stock", "stub")]


def test_restock_creates_event_and_dry_run_alerts(conn, settings):
    # A Labubu US product so it also matches the demo premium subscriber's filters.
    product = db.one(conn, "SELECT * FROM products WHERE sku = ?", ("PM-LAB-MAC-01",))
    events_before = db.one(conn, "SELECT COUNT(*) c FROM restock_events")["c"]

    summary = run_cycle(conn, settings, monitor=StubMonitor(product["id"], product["retail_price"]))

    assert summary["events_created"] == 1
    assert summary["by_type"] == {"restock": 1}
    # Three broadcast channels (telegram/discord/email), all dry-run here.
    assert summary["broadcasts"] == 3
    assert summary["premium_matches"] >= 1

    events_after = db.one(conn, "SELECT COUNT(*) c FROM restock_events")["c"]
    assert events_after == events_before + 1

    logged = db.q(conn, "SELECT * FROM alerts_log WHERE channel='telegram'")
    assert logged and all(r["status"] == "dry_run" for r in logged)

    # A premium match should be logged against a subscriber.
    premium = db.q(conn, "SELECT * FROM alerts_log WHERE channel='premium'")
    assert premium and premium[0]["subscriber_id"] is not None


def test_no_change_creates_no_event(conn, settings):
    class SilentMonitor:
        name = "silent"
        def check(self, products):
            return []

    before = db.one(conn, "SELECT COUNT(*) c FROM restock_events")["c"]
    summary = run_cycle(conn, settings, monitor=SilentMonitor())
    after = db.one(conn, "SELECT COUNT(*) c FROM restock_events")["c"]
    assert summary["events_created"] == 0
    assert after == before


def test_watch_match_is_logged(conn, settings):
    product = db.one(conn, "SELECT * FROM products WHERE sku = ?", ("PM-LAB-MAC-01",))
    db.execute(conn, "INSERT INTO subscribers (email, tier, created_at) VALUES (?,?,?)",
               ("watcher@example.com", "free", "2026-01-01T00:00:00+00:00"))
    sub = db.one(conn, "SELECT id FROM subscribers WHERE email='watcher@example.com'")
    db.execute(conn, "INSERT INTO watchlist (subscriber_id, product_id, created_at) VALUES (?,?,?)",
               (sub["id"], product["id"], "2026-01-01T00:00:00+00:00"))

    summary = run_cycle(conn, settings,
                        monitor=StubMonitor(product["id"], product["retail_price"]))
    assert summary["watch_matches"] >= 1
    rows = db.q(conn, "SELECT * FROM alerts_log WHERE channel='watch'")
    assert rows and rows[0]["subscriber_id"] == sub["id"]


def test_send_personal_email_skipped_without_resend(settings):
    from drophound.engine.alerts import AlertMessage, send_personal_email
    r = send_personal_email(settings, "x@y.com", AlertMessage(title="t", text="hi"))
    assert r.status == "skipped"


class FakeEmailClient:
    def __init__(self):
        self.sent = []

    def post(self, url, json=None, headers=None):
        self.sent.append(json)
        return None


def test_personal_email_delivery(conn, monkeypatch):
    monkeypatch.setenv("RESEND_API_KEY", "re_test")
    monkeypatch.setenv("DROPHOUND_EMAIL_FROM", "onboarding@resend.dev")
    # Keep the broadcast email dispatcher off the network; we're testing the
    # per-person path, which uses the injected fake client.
    import drophound.engine.alerts as alerts_mod
    monkeypatch.setattr(alerts_mod.EmailDispatcher, "configured", lambda self: False)
    from drophound.config import get_settings
    settings = get_settings()  # fresh: now sees the Resend key, same temp DB

    product = db.one(conn, "SELECT * FROM products WHERE sku = ?", ("PM-LAB-MAC-01",))
    db.execute(conn, "INSERT INTO subscribers (email, tier, created_at) VALUES (?,?,?)",
               ("picker@example.com", "free", "2026-01-01T00:00:00+00:00"))
    sub = db.one(conn, "SELECT id FROM subscribers WHERE email='picker@example.com'")
    db.execute(conn, "INSERT INTO watchlist (subscriber_id, product_id, created_at) VALUES (?,?,?)",
               (sub["id"], product["id"], "2026-01-01T00:00:00+00:00"))

    fake = FakeEmailClient()
    summary = run_cycle(conn, settings, email_client=fake,
                        monitor=StubMonitor(product["id"], product["retail_price"]))

    # Both the per-product watcher and the seeded premium filter-matcher get a
    # personal email for this Labubu/US restock.
    assert summary["personal_emails"] >= 1
    emailed = {r["to"][0] for r in fake.sent}
    assert "picker@example.com" in emailed
    sent_rows = db.q(conn, "SELECT * FROM alerts_log WHERE channel='email' AND status='sent'")
    assert sub["id"] in {r["subscriber_id"] for r in sent_rows}
