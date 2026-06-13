"""Stripe billing — hosted Checkout + signed webhook verification.

Uses Stripe's REST API directly over httpx (no extra SDK dependency) and verifies
webhook signatures with stdlib hmac, so the dependency surface stays tiny.

Flow:
  1. User submits the upgrade form -> create_checkout_session() -> redirect to Stripe.
  2. Stripe hosts the card form (PCI handled by them) and charges the subscription.
  3. Stripe POSTs `checkout.session.completed` to /stripe/webhook -> grant premium.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import Any

from .config import Settings


def create_checkout_session(settings: Settings, email: str, *,
                            client: Any | None = None) -> tuple[str | None, dict]:
    """Create a Stripe Checkout Session for the $X/mo subscription.

    Returns (checkout_url, raw_response). Redirect the user to checkout_url.
    """
    base = settings.base_url.rstrip("/")
    data = {
        "mode": "subscription",
        "line_items[0][price]": settings.stripe_price_id,
        "line_items[0][quantity]": "1",
        "success_url": f"{base}/upgrade/success?session_id={{CHECKOUT_SESSION_ID}}",
        "cancel_url": f"{base}/pricing",
        "customer_email": email,
        "client_reference_id": email,
        "allow_promotion_codes": "true",
    }
    headers = {"Authorization": f"Bearer {settings.stripe_secret_key}"}
    url = "https://api.stripe.com/v1/checkout/sessions"
    if client is not None:
        body = client.post(url, data=data, headers=headers).json()
    else:
        import httpx
        with httpx.Client(timeout=30.0) as c:
            resp = c.post(url, data=data, headers=headers)
            resp.raise_for_status()
            body = resp.json()
    return body.get("url"), body


def verify_webhook(payload: bytes, sig_header: str, secret: str,
                   *, tolerance: int = 300, now: float | None = None) -> dict | None:
    """Verify a Stripe webhook signature and return the parsed event, else None.

    Implements Stripe's scheme: signed_payload = "{t}.{raw_body}", compared against
    the v1 HMAC-SHA256 in the Stripe-Signature header.
    """
    if not payload or not sig_header or not secret:
        return None
    parts = dict(p.split("=", 1) for p in sig_header.split(",") if "=" in p)
    t, v1 = parts.get("t"), parts.get("v1")
    if not t or not v1:
        return None
    signed = t.encode() + b"." + payload
    expected = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, v1):
        return None
    if tolerance:
        try:
            if abs((now or time.time()) - int(t)) > tolerance:
                return None
        except ValueError:
            return None
    try:
        return json.loads(payload.decode("utf-8"))
    except Exception:
        return None


def sign_payload(payload: bytes, secret: str, *, timestamp: int | None = None) -> str:
    """Build a Stripe-Signature header value (used in tests and for local signing)."""
    t = timestamp or int(time.time())
    sig = hmac.new(secret.encode(), f"{t}.".encode() + payload, hashlib.sha256).hexdigest()
    return f"t={t},v1={sig}"
