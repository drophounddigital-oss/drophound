"""Environment-driven settings.

Settings are read fresh from the environment each time `get_settings()` is
called, so tests can point `DROPHOUND_DB_PATH` at a temp file per-case and the
web app will pick it up. A local `.env` at the repo root is auto-loaded.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent          # .../drophound/drophound
REPO_DIR = PACKAGE_DIR.parent                           # .../drophound
FIXTURES_DIR = PACKAGE_DIR / "fixtures"
TEMPLATES_DIR = PACKAGE_DIR / "web" / "templates"
STATIC_DIR = PACKAGE_DIR / "web" / "static"
DEFAULT_DB_PATH = REPO_DIR / "var" / "drophound.db"

_ENV_LOADED = False


def _load_dotenv() -> None:
    """Minimal .env loader (no python-dotenv dependency)."""
    global _ENV_LOADED
    if _ENV_LOADED:
        return
    _ENV_LOADED = True
    env_path = REPO_DIR / ".env"
    if not env_path.exists():
        return
    for raw in env_path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        # Existing real env vars win over the file.
        os.environ.setdefault(key, value)


def _get(name: str, default: str | None = None) -> str | None:
    val = os.environ.get(name)
    return val if val not in (None, "") else default


@dataclass(frozen=True)
class Settings:
    db_path: Path
    base_url: str
    premium_price: float
    digest_model: str
    monitor_interval: int
    http_delay: float

    # Channels (None => that channel runs in dry-run / console mode)
    telegram_bot_token: str | None
    telegram_chat_id: str | None
    discord_webhook_url: str | None
    resend_api_key: str | None
    email_from: str | None
    email_to: str | None

    # Resale + affiliate
    ebay_app_id: str | None
    ebay_campaign_id: str | None
    popmart_affiliate_ref: str | None
    stockx_affiliate_ref: str | None

    # Billing + AI
    stripe_secret_key: str | None
    stripe_price_id: str | None
    stripe_webhook_secret: str | None
    anthropic_api_key: str | None

    # Shared secret protecting the inbound /hook/restock webhook
    hook_secret: str | None

    @property
    def has_anthropic(self) -> bool:
        return bool(self.anthropic_api_key)

    @property
    def has_stripe(self) -> bool:
        return bool(self.stripe_secret_key and self.stripe_price_id)


def get_settings() -> Settings:
    _load_dotenv()
    db_path = Path(_get("DROPHOUND_DB_PATH", str(DEFAULT_DB_PATH)))
    return Settings(
        db_path=db_path,
        base_url=_get("DROPHOUND_BASE_URL", "http://localhost:8000"),
        premium_price=float(_get("DROPHOUND_PREMIUM_PRICE", "8")),
        digest_model=_get("DROPHOUND_DIGEST_MODEL", "claude-sonnet-4-6"),
        monitor_interval=int(_get("DROPHOUND_MONITOR_INTERVAL", "300")),
        http_delay=float(_get("DROPHOUND_HTTP_DELAY", "2")),
        telegram_bot_token=_get("TELEGRAM_BOT_TOKEN"),
        telegram_chat_id=_get("TELEGRAM_CHAT_ID"),
        discord_webhook_url=_get("DISCORD_WEBHOOK_URL"),
        resend_api_key=_get("RESEND_API_KEY"),
        email_from=_get("DROPHOUND_EMAIL_FROM", "onboarding@resend.dev"),
        email_to=_get("DROPHOUND_EMAIL_TO"),
        ebay_app_id=_get("EBAY_APP_ID"),
        ebay_campaign_id=_get("EBAY_CAMPAIGN_ID"),
        popmart_affiliate_ref=_get("POPMART_AFFILIATE_REF"),
        stockx_affiliate_ref=_get("STOCKX_AFFILIATE_REF"),
        stripe_secret_key=_get("STRIPE_SECRET_KEY"),
        stripe_price_id=_get("STRIPE_PRICE_ID"),
        stripe_webhook_secret=_get("STRIPE_WEBHOOK_SECRET"),
        anthropic_api_key=_get("ANTHROPIC_API_KEY"),
        hook_secret=_get("DROPHOUND_HOOK_SECRET"),
    )
