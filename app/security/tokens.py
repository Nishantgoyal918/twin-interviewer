"""Invite tokens (long-lived share links, hashed in DB) and session tokens (short-lived HMAC).

The invite token only starts a session; every later call uses the session token (plan 8.2).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass

from app.settings import get_settings


def new_invite_token() -> tuple[str, str]:
    """Returns (token for the link, hash to store)."""
    token = secrets.token_urlsafe(24)
    return token, hash_invite(token)


def hash_invite(token: str) -> str:
    secret = get_settings().invite_token_secret.encode()
    return hmac.new(secret, token.encode(), hashlib.sha256).hexdigest()


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


@dataclass(frozen=True)
class SessionClaims:
    session_id: str
    twin_id: str
    exp: int


class BadToken(ValueError):
    pass


def sign_session(session_id: str, twin_id: str, ttl_s: int) -> str:
    payload = _b64(json.dumps({"sid": session_id, "tid": twin_id,
                               "exp": int(time.time()) + ttl_s}).encode())
    sig = _b64(hmac.new(_key(), payload.encode(), hashlib.sha256).digest())
    return f"{payload}.{sig}"


def verify_session(token: str) -> SessionClaims:
    try:
        payload, sig = token.split(".", 1)
    except ValueError as exc:
        raise BadToken("malformed") from exc
    expected = _b64(hmac.new(_key(), payload.encode(), hashlib.sha256).digest())
    if not hmac.compare_digest(sig, expected):
        raise BadToken("bad signature")
    data = json.loads(_unb64(payload))
    if data["exp"] < time.time():
        raise BadToken("expired")
    return SessionClaims(session_id=data["sid"], twin_id=data["tid"], exp=data["exp"])


def _key() -> bytes:
    return ("session:" + get_settings().invite_token_secret).encode()
