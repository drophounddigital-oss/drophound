"""Shared pytest fixtures. Each test gets an isolated, seeded temp database."""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient


# External-channel credentials are blanked so tests are hermetic: every channel
# runs in dry-run and no test ever makes a real network call, even if the
# developer has a populated .env (real Telegram/eBay/Anthropic keys, etc.).
_NEUTRALIZE = (
    "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "DISCORD_WEBHOOK_URL",
    "RESEND_API_KEY", "DROPHOUND_EMAIL_TO", "EBAY_APP_ID", "EBAY_CAMPAIGN_ID",
    "ANTHROPIC_API_KEY", "STRIPE_SECRET_KEY", "STRIPE_PRICE_ID",
    "STRIPE_WEBHOOK_SECRET", "DROPHOUND_HOOK_SECRET",
)


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.setenv("DROPHOUND_DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("DROPHOUND_BASE_URL", "http://testserver")
    for var in _NEUTRALIZE:
        monkeypatch.setenv(var, "")
    from drophound.config import get_settings
    return get_settings()


@pytest.fixture
def conn(settings):
    from drophound import db, seed
    c = db.connect(settings.db_path)
    seed.seed(c, settings, reset=True)
    yield c
    c.close()


@pytest.fixture
def client(settings, conn):
    # `conn` has already created + seeded the temp database at settings.db_path.
    from drophound.web.app import app
    with TestClient(app) as tc:
        yield tc
