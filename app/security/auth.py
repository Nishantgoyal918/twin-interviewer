"""FastAPI auth dependencies: creator (Supabase JWT) and subject (session token)."""

from __future__ import annotations

import httpx
from fastapi import Header, HTTPException, Request

from app import db
from app.security.tokens import BadToken, SessionClaims, verify_session
from app.settings import get_settings

_user_memo: dict[str, str] = {}
SHARED_CREATOR = "00000000-0000-0000-0000-00000000c0de"  # everyone, when OPEN_ACCESS=1


def creator_id(
    authorization: str | None = Header(default=None),
    x_dev_creator: str | None = Header(default=None),
) -> str:
    """Returns the creator's user id, upserting the creators row on first sight.

    OPEN_ACCESS=1 (public demo): no sign-in at all. Every visitor is the same shared creator,
    so everyone sees and uses the same twins. Spend caps still apply to everyone together."""
    s = get_settings()
    if s.dev_auth and x_dev_creator:
        uid = x_dev_creator
    elif s.open_access:
        uid = SHARED_CREATOR
    else:
        if not authorization or not authorization.lower().startswith("bearer "):
            raise HTTPException(401, "Sign in to continue.")
        token = authorization.split(" ", 1)[1]
        uid = _user_memo.get(token) or _verify_supabase(token)
        _user_memo[token] = uid
    with db.tx() as conn:
        conn.execute(
            "insert into creators (id) values (%s) on conflict (id) do nothing", (uid,)
        )
    return uid


def _verify_supabase(token: str) -> str:
    s = get_settings()
    r = httpx.get(
        f"{s.supabase_url}/auth/v1/user",
        headers={"Authorization": f"Bearer {token}", "apikey": s.supabase_anon_key},
        timeout=10,
    )
    if r.status_code != 200:
        raise HTTPException(401, "Your sign-in expired. Sign in again.")
    return r.json()["id"]


def session_claims(request: Request, authorization: str | None = Header(default=None)) -> SessionClaims:
    token = None
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization.split(" ", 1)[1]
    token = token or request.query_params.get("st")
    if not token:
        raise HTTPException(401, "Missing session token.")
    try:
        return verify_session(token)
    except BadToken as exc:
        raise HTTPException(401, "This chat link has expired. Open the invite link again.") from exc


def own_twin(conn, twin_id: str, uid: str) -> dict:
    row = db.one(conn, "select * from twins where id = %s and creator_id = %s", (twin_id, uid))
    if not row:
        raise HTTPException(404, "Twin not found.")
    return row
