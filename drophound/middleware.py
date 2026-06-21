"""Custom Starlette middleware: rate limiting, CORS, UUID sessions, logging."""

from __future__ import annotations

import logging
import time
import uuid
from collections import defaultdict, deque
from typing import Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

logger = logging.getLogger("drophound")

# --------------------------------------------------------------------------- #
# Rate limiter
# --------------------------------------------------------------------------- #
_RATE_WINDOWS: dict[str, deque] = defaultdict(deque)

# (max_requests, window_seconds) per route prefix
_LIMITS: list[tuple[str, int, int]] = [
    ("/api/",        60,  60),   # 60 req/min on JSON API
    ("/subscribe",   5,   60),   # 5 signups/min
    ("/upgrade",     5,   60),   # 5 checkout attempts/min
    ("/watch/add",   30,  60),   # 30 watch toggles/min
    ("/watch/remove",30,  60),
    ("/stripe/",     20,  60),
    ("/hook/",       20,  60),
]
_GLOBAL_LIMIT = (300, 60)       # 300 req/min globally per IP


def _rate_key(request: Request, suffix: str = "") -> str:
    ip = (
        request.headers.get("x-forwarded-for", "").split(",")[0].strip()
        or request.client.host
        or "unknown"
    )
    return f"{ip}:{suffix}"


def _check_rate(key: str, max_req: int, window: int, now: float) -> bool:
    """Return True if request is allowed, False if rate-limited."""
    dq = _RATE_WINDOWS[key]
    while dq and dq[0] < now - window:
        dq.popleft()
    if len(dq) >= max_req:
        return False
    dq.append(now)
    return True


class RateLimitMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        now = time.time()
        path = request.url.path

        # Global per-IP limit
        if not _check_rate(_rate_key(request, "global"), *_GLOBAL_LIMIT, now):
            logger.warning("rate_limit global ip=%s path=%s", _rate_key(request), path)
            return JSONResponse(
                {"error": "Too many requests. Slow down and try again."},
                status_code=429,
                headers={"Retry-After": "60"},
            )

        # Per-endpoint limits
        for prefix, max_req, window in _LIMITS:
            if path.startswith(prefix):
                if not _check_rate(_rate_key(request, prefix), max_req, window, now):
                    logger.warning("rate_limit endpoint=%s ip=%s", prefix, _rate_key(request))
                    return JSONResponse(
                        {"error": "Too many requests on this endpoint. Try again in a minute."},
                        status_code=429,
                        headers={"Retry-After": "60"},
                    )
                break

        return await call_next(request)


# --------------------------------------------------------------------------- #
# UUID session cookie  (identifies browser across requests; no passwords stored)
# --------------------------------------------------------------------------- #
SESSION_COOKIE = "dh_sid"
SESSION_MAX_AGE = 60 * 60 * 24 * 365  # 1 year


def get_session_id(request: Request) -> str:
    """Return the browser's session UUID from the cookie (or a fresh one)."""
    return request.cookies.get(SESSION_COOKIE) or str(uuid.uuid4())


class SessionMiddleware(BaseHTTPMiddleware):
    """Attach a durable UUID to every browser. Sets the cookie on responses."""

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        sid = get_session_id(request)
        request.state.session_id = sid
        response = await call_next(request)
        # Refresh the cookie (keeps it alive on each visit)
        if not request.cookies.get(SESSION_COOKIE):
            response.set_cookie(
                SESSION_COOKIE, sid,
                max_age=SESSION_MAX_AGE,
                httponly=True,
                samesite="lax",
                secure=request.url.scheme == "https",
            )
        return response


# --------------------------------------------------------------------------- #
# Structured request logging
# --------------------------------------------------------------------------- #
class LoggingMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        start = time.perf_counter()
        response = await call_next(request)
        ms = (time.perf_counter() - start) * 1000
        path = request.url.path
        # Skip noisy static / health pings
        if not path.startswith("/static") and path != "/api/health":
            logger.info(
                "req method=%s path=%s status=%s ms=%.1f ip=%s",
                request.method, path, response.status_code, ms,
                _rate_key(request),
            )
        return response
