"""Firebase Firestore integration — optional user data sync and audit trail.

Activated when FIREBASE_CREDENTIALS_JSON is set to the service-account JSON
string (downloaded from Firebase Console → Project Settings → Service Accounts).

Falls back to a no-op when:
  - The env var is not set
  - firebase-admin is not installed
  - Firebase initialisation fails for any reason

This means the app boots and runs fully without Firebase credentials;
Firestore is an additive security / audit layer, not a dependency.

Data layout
-----------
users/{email}/
  email, tier, created_at, last_login, ...

users/{email}/security_events/{auto-id}
  type, ts, ip, user_agent, detail, ...
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger("drophound.firebase")

_app  = None   # firebase_admin app
_db   = None   # firestore.Client


def init(credentials_json: str | None, project_id: str | None = None) -> None:
    """Initialize Firebase Admin SDK. Safe to call multiple times."""
    global _app, _db
    if not credentials_json:
        return
    try:
        import firebase_admin
        from firebase_admin import credentials, firestore as fs

        if not firebase_admin._apps:
            cred_dict = json.loads(credentials_json)
            cred = credentials.Certificate(cred_dict)
            _app = firebase_admin.initialize_app(
                cred,
                {"projectId": project_id or cred_dict.get("project_id")},
            )
        _db = fs.client()
        logger.info("firebase: Firestore client initialised")
    except ImportError:
        logger.warning("firebase: firebase-admin not installed — Firestore disabled")
    except Exception as exc:
        logger.warning("firebase: init failed (%s) — Firestore disabled", exc)


def _users():
    return _db.collection("users") if _db else None


# --------------------------------------------------------------------------- #
# Public helpers
# --------------------------------------------------------------------------- #

def upsert_user(email: str, data: dict[str, Any]) -> None:
    """Create or merge user document in Firestore."""
    col = _users()
    if not col:
        return
    try:
        from google.cloud.firestore_v1 import SERVER_TIMESTAMP
        col.document(email).set({"updated_at": SERVER_TIMESTAMP, **data}, merge=True)
    except Exception as exc:
        logger.debug("firebase: upsert_user failed — %s", exc)


def log_event(
    email: str,
    event_type: str,
    *,
    ip: str | None = None,
    user_agent: str | None = None,
    detail: str | None = None,
) -> None:
    """Append a security event to users/{email}/security_events."""
    col = _users()
    if not col:
        return
    try:
        payload: dict[str, Any] = {
            "type": event_type,
            "ts": datetime.now(timezone.utc).isoformat(),
        }
        if ip:
            payload["ip"] = ip
        if user_agent:
            payload["user_agent"] = user_agent[:200]
        if detail:
            payload["detail"] = detail
        col.document(email).collection("security_events").add(payload)
    except Exception as exc:
        logger.debug("firebase: log_event failed — %s", exc)


def get_user(email: str) -> dict[str, Any] | None:
    """Return the Firestore user document dict, or None."""
    col = _users()
    if not col:
        return None
    try:
        doc = col.document(email).get()
        return doc.to_dict() if doc.exists else None
    except Exception as exc:
        logger.debug("firebase: get_user failed — %s", exc)
        return None


def delete_user(email: str) -> None:
    """Remove user document (and security_events sub-collection) from Firestore."""
    col = _users()
    if not col:
        return
    try:
        ref = col.document(email)
        # Delete sub-collection first (Firestore doesn't cascade)
        for doc in ref.collection("security_events").stream():
            doc.reference.delete()
        ref.delete()
        logger.info("firebase: deleted user %s", email)
    except Exception as exc:
        logger.debug("firebase: delete_user failed — %s", exc)
