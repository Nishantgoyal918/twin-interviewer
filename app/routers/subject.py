"""Subject API: invite → session token, voice connect, turns, delegation, uploads, end."""

from __future__ import annotations

import json
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from app import db
from app.conversation import create_live_session
from app.security.auth import session_claims
from app.security.tokens import BadToken, SessionClaims, verify_session
from app.services import context, sessions, turns
from app.settings import get_settings
from app.storage import storage

router = APIRouter(prefix="/api")


def client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for", "")
    return fwd.split(",")[0].strip() or (request.client.host if request.client else "unknown")


@router.get("/invites/{token}")
def invite(token: str) -> dict:
    return sessions.invite_info(token)


class StartIn(BaseModel):
    invite_token: str
    modality: str = Field(pattern="^(voice|text)$")


@router.post("/sessions")
def start(body: StartIn, request: Request) -> dict:
    return sessions.start(body.invite_token, body.modality, client_ip(request))


def _check(claims: SessionClaims, session_id: str) -> None:
    if claims.session_id != session_id:
        raise HTTPException(403, "Wrong session.")


@router.get("/sessions/{session_id}/history")
def history(session_id: str, claims: SessionClaims = Depends(session_claims)) -> list[dict]:
    _check(claims, session_id)
    with db.tx() as conn:
        rows = db.all_(conn, """select id, idx, speaker, text_live text from turns
                                where session_id=%s order by idx""", (session_id,))
    return [{**r, "id": str(r["id"])} for r in rows]


@router.post("/sessions/{session_id}/end")
async def end(session_id: str, request: Request) -> dict:
    # Also called by navigator.sendBeacon (no headers, text/plain body).
    raw = await request.body()
    try:
        data = json.loads(raw or b"{}")
    except ValueError:
        data = {}
    token = data.get("session_token")
    auth = request.headers.get("authorization", "")
    if not token and auth.lower().startswith("bearer "):
        token = auth.split(" ", 1)[1]
    try:
        claims = verify_session(token or "")
    except BadToken as exc:
        raise HTTPException(401, "Missing or expired session token.") from exc
    _check(claims, session_id)
    reason = data.get("reason") if data.get("reason") in ("user", "planner_wrap", "max_duration",
                                                          "tab_closed") else "user"
    exp = data.get("expected_segments")
    return sessions.end_session(session_id, reason=reason,
                                expected_segments=int(exp) if exp is not None else None)


class ConnectIn(BaseModel):
    sdp: str = Field(max_length=100_000)


@router.post("/voice/connect")
def voice_connect(body: ConnectIn, claims: SessionClaims = Depends(session_claims)) -> dict:
    sid, twin_id = claims.session_id, claims.twin_id
    with db.tx() as conn:
        s = db.one(conn, "select * from sessions where id=%s", (sid,))
        if not s or s["status"] != "active" or s["modality"] != "voice":
            raise HTTPException(409, "This voice chat isn't active.")
        spec = context.spec_for_session(conn, s)
        state = context.load_state(conn, s, spec)
        notes = context.notes_text(conn, twin_id)
        dnr = [r["topic"] for r in db.all_(conn, "select topic from do_not_raise where twin_id=%s",
                                           (twin_id,))]
    voice = None
    if get_settings().dev_auth or get_settings().sim_key:
        from app.routers.dev import interviewer_voice

        voice = interviewer_voice(twin_id)
    live = create_live_session(spec, sdp_offer=body.sdp, notes=notes, do_not_raise=dnr,
                               guidance=state.guidance,
                               first_session=bool((s["stats"] or {}).get("first_session")),
                               voice=voice)
    with db.tx() as conn:
        conn.execute("update sessions set gptlive_session_ref=%s where id=%s", (live["id"], sid))
    return {"sdp": live["sdp"], "live_session_id": live["id"],
            "max_duration_s": s["max_duration_s"], "elapsed_s": context.elapsed_s(s)}


@router.post("/turn")
def post_turn(body: turns.TurnIn, claims: SessionClaims = Depends(session_claims)) -> dict:
    return turns.post_turn(claims.session_id, body)


@router.post("/turn/{turn_id}/analyze")
def analyze(turn_id: str, claims: SessionClaims = Depends(session_claims)) -> dict:
    return turns.analyze(claims.session_id, turn_id)


class DelegationIn(BaseModel):
    delegation_id: str | None = None
    last_subject_text: str = Field(default="", max_length=4000)


@router.post("/delegation")
def delegation(body: DelegationIn, claims: SessionClaims = Depends(session_claims)) -> dict:
    content = turns.delegation(claims.session_id, claims.twin_id, body.last_subject_text)
    return {"delegation_id": body.delegation_id, "content": content}


class SignIn(BaseModel):
    seq: int = Field(ge=0, le=10_000)
    kind: Literal["segment", "call"] = "segment"   # mic chunk for transcription | whole-call part
    content_type: str = "audio/webm"


@router.post("/uploads/sign")
def upload_sign(body: SignIn, claims: SessionClaims = Depends(session_claims)) -> dict:
    ext = "ogg" if "ogg" in body.content_type else "mp4" if "mp4" in body.content_type else "webm"
    name = f"{body.seq:04d}" if body.kind == "segment" else f"call-{body.seq:04d}"
    path = f"audio/{claims.twin_id}/{claims.session_id}/{name}.{ext}"
    return {"path": path, **storage().signed_upload(path, body.content_type)}


class CompleteIn(BaseModel):
    seq: int
    kind: Literal["segment", "call"] = "segment"
    path: str
    start_ms: int
    end_ms: int | None = None
    bytes: int | None = None


@router.post("/uploads/complete")
def upload_complete(body: CompleteIn, claims: SessionClaims = Depends(session_claims)) -> dict:
    prefix = f"audio/{claims.twin_id}/{claims.session_id}/"
    if not body.path.startswith(prefix):
        raise HTTPException(400, "Bad upload path.")
    with db.tx() as conn:
        conn.execute(
            """insert into audio_assets (session_id, kind, seq, start_ms, end_ms, storage_path, bytes)
               values (%s,%s,%s,%s,%s,%s,%s)
               on conflict (session_id, kind, seq) do update set end_ms=excluded.end_ms,
                 storage_path=excluded.storage_path, bytes=excluded.bytes""",
            (claims.session_id, body.kind, body.seq, body.start_ms, body.end_ms, body.path,
             body.bytes),
        )
    return {"ok": True}
