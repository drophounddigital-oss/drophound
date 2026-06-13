import json

from drophound import billing, db


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


class FakeStripe:
    """Captures the params create_checkout_session sends to Stripe."""
    def __init__(self, url="https://checkout.stripe.com/c/pay/cs_test_123"):
        self.url = url
        self.calls = []

    def post(self, url, data=None, headers=None):
        self.calls.append((url, data, headers))
        return _Resp({"url": self.url, "id": "cs_test_123"})


# --- Checkout session -------------------------------------------------------
def test_create_checkout_session_params(monkeypatch):
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_x")
    monkeypatch.setenv("STRIPE_PRICE_ID", "price_123")
    monkeypatch.setenv("DROPHOUND_BASE_URL", "https://example.com")
    from drophound.config import get_settings
    settings = get_settings()
    fake = FakeStripe()

    url, body = billing.create_checkout_session(settings, "buyer@example.com", client=fake)
    assert url.startswith("https://checkout.stripe.com")
    _, data, headers = fake.calls[0]
    assert data["mode"] == "subscription"
    assert data["line_items[0][price]"] == "price_123"
    assert data["customer_email"] == "buyer@example.com"
    assert data["client_reference_id"] == "buyer@example.com"
    assert "example.com/upgrade/success" in data["success_url"]
    assert headers["Authorization"] == "Bearer sk_test_x"


# --- Webhook signature verification ----------------------------------------
def test_verify_webhook_roundtrip():
    payload = b'{"type":"checkout.session.completed","data":{"object":{}}}'
    sig = billing.sign_payload(payload, "whsec_x")
    event = billing.verify_webhook(payload, sig, "whsec_x")
    assert event and event["type"] == "checkout.session.completed"


def test_verify_webhook_wrong_secret_rejected():
    payload = b'{"a":1}'
    sig = billing.sign_payload(payload, "whsec_x")
    assert billing.verify_webhook(payload, sig, "whsec_WRONG") is None


def test_verify_webhook_stale_timestamp_rejected():
    payload = b'{"a":1}'
    sig = billing.sign_payload(payload, "whsec_x", timestamp=1)  # 1970
    assert billing.verify_webhook(payload, sig, "whsec_x") is None


# --- Endpoints --------------------------------------------------------------
def test_stripe_webhook_marks_premium(client, conn, monkeypatch):
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec_test")
    payload = json.dumps({
        "type": "checkout.session.completed",
        "data": {"object": {"client_reference_id": "paid@example.com",
                            "customer": "cus_123"}},
    }).encode()
    sig = billing.sign_payload(payload, "whsec_test")
    r = client.post("/stripe/webhook", content=payload,
                    headers={"Stripe-Signature": sig, "Content-Type": "application/json"})
    assert r.status_code == 200 and r.json()["received"] is True
    row = db.one(conn, "SELECT tier, stripe_customer_id FROM subscribers WHERE email='paid@example.com'")
    assert row and row["tier"] == "premium" and row["stripe_customer_id"] == "cus_123"


def test_stripe_webhook_rejects_bad_signature(client):
    r = client.post("/stripe/webhook", content=b'{"type":"x"}',
                    headers={"Stripe-Signature": "t=1,v1=deadbeef"})
    assert r.status_code == 400


def test_upgrade_demo_flip_without_stripe(client, conn):
    # No Stripe configured -> the demo path still flips the tier locally.
    r = client.post("/upgrade", data={"email": "demoflip@example.com"})
    assert r.status_code == 200
    row = db.one(conn, "SELECT tier FROM subscribers WHERE email='demoflip@example.com'")
    assert row and row["tier"] == "premium"
