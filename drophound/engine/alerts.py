"""Alert delivery — Telegram, Discord, email.

Each dispatcher sends for real when its credentials are configured and otherwise
returns a `dry_run` result (and prints to the console), so a fresh checkout
demonstrates the full alert flow without any secrets. The pipeline logs every
attempt to `alerts_log`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..config import Settings


@dataclass
class AlertMessage:
    title: str
    text: str
    url: str | None = None
    event_id: int | None = None
    product_id: int | None = None

    def as_plaintext(self) -> str:
        return f"{self.text}" + (f"\n{self.url}" if self.url else "")


@dataclass
class DispatchResult:
    channel: str
    status: str          # sent | dry_run | failed
    detail: str = ""


class _Base:
    channel = "base"

    def configured(self) -> bool:
        raise NotImplementedError

    def _send(self, message: AlertMessage) -> DispatchResult:
        raise NotImplementedError

    def send(self, message: AlertMessage) -> DispatchResult:
        if not self.configured():
            print(f"  [dry-run · {self.channel}] {message.text}")
            return DispatchResult(self.channel, "dry_run", "no credentials configured")
        try:
            return self._send(message)
        except Exception as exc:  # never let a channel failure crash the cycle
            print(f"  [failed · {self.channel}] {exc}")
            return DispatchResult(self.channel, "failed", str(exc))


class TelegramDispatcher(_Base):
    channel = "telegram"

    def __init__(self, settings: Settings, *, client: Any | None = None):
        self.settings = settings
        self._client = client

    def configured(self) -> bool:
        return bool(self.settings.telegram_bot_token and self.settings.telegram_chat_id)

    def _send(self, message: AlertMessage) -> DispatchResult:
        url = f"https://api.telegram.org/bot{self.settings.telegram_bot_token}/sendMessage"
        payload = {
            "chat_id": self.settings.telegram_chat_id,
            "text": message.as_plaintext(),
            "disable_web_page_preview": False,
        }
        self._post(url, payload)
        return DispatchResult(self.channel, "sent")

    def _post(self, url: str, payload: dict) -> None:
        if self._client is not None:
            self._client.post(url, json=payload)
            return
        import httpx
        with httpx.Client(timeout=15.0) as c:
            c.post(url, json=payload).raise_for_status()


class DiscordDispatcher(_Base):
    channel = "discord"

    def __init__(self, settings: Settings, *, client: Any | None = None):
        self.settings = settings
        self._client = client

    def configured(self) -> bool:
        return bool(self.settings.discord_webhook_url)

    def _send(self, message: AlertMessage) -> DispatchResult:
        content = f"**{message.title}**\n{message.as_plaintext()}"
        self._post(self.settings.discord_webhook_url, {"content": content})
        return DispatchResult(self.channel, "sent")

    def _post(self, url: str, payload: dict) -> None:
        if self._client is not None:
            self._client.post(url, json=payload)
            return
        import httpx
        with httpx.Client(timeout=15.0) as c:
            c.post(url, json=payload).raise_for_status()


class EmailDispatcher(_Base):
    """Broadcast preview via Resend. A production build would fan out to the list;
    here it sends a single preview to the configured from-address."""

    channel = "email"

    def __init__(self, settings: Settings, *, client: Any | None = None):
        self.settings = settings
        self._client = client

    def configured(self) -> bool:
        return bool(self.settings.resend_api_key and self.settings.email_from)

    def _send(self, message: AlertMessage) -> DispatchResult:
        recipient = self.settings.email_to or self.settings.email_from
        payload = {
            "from": self.settings.email_from,
            "to": [recipient],
            "subject": message.title,
            "text": message.as_plaintext(),
        }
        headers = {"Authorization": f"Bearer {self.settings.resend_api_key}"}
        url = "https://api.resend.com/emails"
        if self._client is not None:
            self._client.post(url, json=payload, headers=headers)
        else:
            import httpx
            with httpx.Client(timeout=20.0) as c:
                c.post(url, json=payload, headers=headers).raise_for_status()
        return DispatchResult(self.channel, "sent")


def broadcast_dispatchers(settings: Settings) -> list[_Base]:
    """The free-funnel broadcast channels (Layer 1)."""
    return [
        TelegramDispatcher(settings),
        DiscordDispatcher(settings),
        EmailDispatcher(settings),
    ]


def send_personal_email(settings: Settings, to_email: str, message: AlertMessage,
                        *, reason: str = "watch", client: Any | None = None) -> DispatchResult:
    """Send one drop alert to a single subscriber's own inbox (Resend).

    This is the per-person delivery path: each watcher/filter-matcher gets an
    email for only the items they chose. Returns 'skipped' when Resend isn't
    configured, 'failed' on API error (e.g. Resend free tier only allows the
    account address until a domain is verified), 'sent' otherwise.
    """
    if not (settings.resend_api_key and settings.email_from):
        return DispatchResult("email", "skipped", "resend not configured")
    why = ("You're watching this drop on DropHound."
           if reason == "watch" else "This matches your DropHound alert filters.")
    payload = {
        "from": settings.email_from,
        "to": [to_email],
        "subject": message.title,
        "text": f"{message.as_plaintext()}\n\n{why}",
    }
    headers = {"Authorization": f"Bearer {settings.resend_api_key}"}
    url = "https://api.resend.com/emails"
    try:
        if client is not None:
            client.post(url, json=payload, headers=headers)
        else:
            import httpx
            with httpx.Client(timeout=20.0) as c:
                c.post(url, json=payload, headers=headers).raise_for_status()
        return DispatchResult("email", "sent", to_email)
    except Exception as exc:
        return DispatchResult("email", "failed", str(exc)[:140])
