"""Input validation and expiring token helpers."""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import time

# --------------------------------------------------------------------------- #
# Input validators
# --------------------------------------------------------------------------- #
_EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,255}\.[^@\s]{1,63}$")
_SAFE_STR_RE = re.compile(r"[<>\"'\\;]")   # strip SQL/HTML injection chars


def validate_email(value: str) -> str:
    """Return lowercased email or raise ValueError."""
    v = (value or "").strip().lower()
    if not v:
        raise ValueError("Email is required.")
    if len(v) > 320:
        raise ValueError("Email is too long.")
    if not _EMAIL_RE.match(v):
        raise ValueError("Please enter a valid email address.")
    return v


def sanitize_str(value: str, max_len: int = 200) -> str:
    """Strip dangerous characters and truncate."""
    v = _SAFE_STR_RE.sub("", (value or "").strip())
    return v[:max_len]


def validate_positive_int(value: str, name: str = "value") -> int:
    try:
        n = int(value)
        if n <= 0:
            raise ValueError
        return n
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a positive integer.")


# --------------------------------------------------------------------------- #
# Expiring signed tokens (for password reset / email verification links)
# --------------------------------------------------------------------------- #
_TOKEN_SEP = "."


def make_token(payload: str, secret: str, ttl_seconds: int = 3600) -> str:
    """Create a signed token: base-hex(payload + expiry) + HMAC."""
    exp = int(time.time()) + ttl_seconds
    body = f"{payload}{_TOKEN_SEP}{exp}"
    sig = hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()[:16]
    return f"{body}{_TOKEN_SEP}{sig}"


def verify_token(token: str, secret: str) -> str | None:
    """Return the payload if the token is valid and unexpired, else None."""
    try:
        parts = token.split(_TOKEN_SEP)
        if len(parts) != 3:
            return None
        payload, exp_str, sig = parts
        body = f"{payload}{_TOKEN_SEP}{exp_str}"
        expected = hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()[:16]
        if not hmac.compare_digest(expected, sig):
            return None
        if time.time() > int(exp_str):
            return None   # expired
        return payload
    except Exception:
        return None


def generate_reset_token() -> str:
    """Random 32-hex token for password reset (used as the payload)."""
    return secrets.token_hex(32)


# --------------------------------------------------------------------------- #
# Password hashing (stdlib scrypt — no external deps)
# --------------------------------------------------------------------------- #

def hash_password(password: str) -> str:
    """Hash a password with scrypt. Format: scrypt:<salt_hex>:<hash_hex>."""
    salt = os.urandom(16)
    h = hashlib.scrypt(password.encode(), salt=salt, n=16384, r=8, p=1)
    return f"scrypt:{salt.hex()}:{h.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """Return True if password matches the stored scrypt hash."""
    try:
        _, salt_hex, hash_hex = stored.split(":")
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(hash_hex)
        actual = hashlib.scrypt(password.encode(), salt=salt, n=16384, r=8, p=1)
        return hmac.compare_digest(actual, expected)
    except Exception:
        return False


def validate_password(value: str) -> str:
    """Return stripped password or raise ValueError."""
    v = (value or "").strip()
    if len(v) < 8:
        raise ValueError("Password must be at least 8 characters.")
    if len(v) > 128:
        raise ValueError("Password must be under 128 characters.")
    return v
