"""Security utilities: CSRF, security headers, and brute-force defense."""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
import time
from collections import defaultdict, deque
from typing import Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

logger = logging.getLogger("drophound.security")

# --------------------------------------------------------------------------- #
# CSRF — stateless HMAC token derived from session ID
# --------------------------------------------------------------------------- #
# Rotate hourly; accept current and previous bucket so tokens survive across
# the hour boundary without requiring server-side storage.
_CSRF_BUCKET_SECONDS = 3600


def csrf_token(session_id: str, secret: str) -> str:
    """Derive a CSRF token from the session ID and current hour bucket."""
    bucket = int(time.time()) // _CSRF_BUCKET_SECONDS
    msg = f"{session_id}:{bucket}".encode()
    return hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest()[:40]


def verify_csrf(token: str, session_id: str, secret: str) -> bool:
    """Return True if the token is valid for this session (current or previous hour)."""
    if not token or not session_id or not secret:
        return False
    now_bucket = int(time.time()) // _CSRF_BUCKET_SECONDS
    for bucket in (now_bucket, now_bucket - 1):
        msg = f"{session_id}:{bucket}".encode()
        expected = hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest()[:40]
        if hmac.compare_digest(expected, token):
            return True
    return False


CSRF_FIELD = "_csrf"
CSRF_HEADER = "x-csrf-token"

# These endpoints receive from third-party servers; CSRF exemption is correct.
_CSRF_EXEMPT = {"/stripe/webhook", "/hook/restock"}


# --------------------------------------------------------------------------- #
# Security headers middleware
# --------------------------------------------------------------------------- #
_CSP = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline'; "
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
    "font-src https://fonts.gstatic.com; "
    "img-src 'self' data: https:; "
    "connect-src 'self'; "
    "frame-ancestors 'none'; "
    "form-action 'self' https://checkout.stripe.com; "
    "base-uri 'self'"
)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        response = await call_next(request)
        h = response.headers
        h["X-Content-Type-Options"] = "nosniff"
        h["X-Frame-Options"] = "DENY"
        h["Referrer-Policy"] = "strict-origin-when-cross-origin"
        h["Permissions-Policy"] = "geolocation=(), camera=(), microphone=(), payment=()"
        h["Content-Security-Policy"] = _CSP
        if request.url.scheme == "https":
            h["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains; preload"
        # Remove server fingerprint
        if "server" in h:
            del h["server"]
        return response


# --------------------------------------------------------------------------- #
# Login brute-force defence — per email, in-memory sliding window
# --------------------------------------------------------------------------- #
_FAILURES: dict[str, deque] = defaultdict(deque)
_LOCKOUT_WINDOW = 900    # 15 minutes
_LOCKOUT_AFTER  = 10     # 10 failures in window = soft lockout


def record_failure(email: str) -> None:
    _FAILURES[email.lower()].append(time.time())


def is_locked(email: str) -> bool:
    now = time.time()
    dq = _FAILURES[email.lower()]
    while dq and dq[0] < now - _LOCKOUT_WINDOW:
        dq.popleft()
    return len(dq) >= _LOCKOUT_AFTER


def clear_failures(email: str) -> None:
    _FAILURES.pop(email.lower(), None)


# --------------------------------------------------------------------------- #
# Honeypot — bots fill every visible and hidden field; humans leave it blank
# --------------------------------------------------------------------------- #
HONEYPOT_FIELD = "website"   # must be styled display:none / off-screen in HTML


def is_bot_submission(form_data) -> bool:
    """Return True if the honeypot field was filled in (bot indicator)."""
    return bool((form_data.get(HONEYPOT_FIELD) or "").strip())
