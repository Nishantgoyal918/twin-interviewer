"""Session lifecycle: start (invite → session token), end, idle sweep (plan 8.2 / 8.4)."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException

from app import db, queue
from app.conversation import text_reply
from app.security import caps
from app.security.tokens import hash_invite, sign_session
from app.services import context
from app.settings import get_settings

log = logging.getLogger("ti.sessions")


def _invite(conn, invite_token: str) -> dict:
    row = db.one(
        conn,
        """select i.*, t.status twin_status, t.subject_name from subject_invites i
           join twins t on t.id = i.twin_id where i.token_hash = %s""",
        (hash_invite(invite_token),),
    )
    if not row or row["revoked_at"] or (row["expires_at"] and row["expires_at"] < datetime.now(UTC)):
        raise HTTPException(404, "This invite link isn't valid any more. Ask for a new one.")
    if row["twin_status"] != "active":
        raise HTTPException(409, "This interview isn't open yet.")
    return row


def invite_info(invite_token: str) -> dict:
    with db.tx() as conn:
        inv = _invite(conn, invite_token)
        found = context.active_spec(conn, str(inv["twin_id"]))
        spec = found[1] if found else None
    return {"subject_name": inv["subject_name"],
            "modalities": spec.modalities if spec else ["text"],
            "audience": spec.audience if spec else ""}


def sweep_idle(twin_id: str | None = None) -> list[str]:
    """Ends sessions nobody closed (closed tab, lost network). Returns ended ids."""
    cfg = get_settings().cfg
    voice_cut = datetime.now(UTC) - timedelta(minutes=int(cfg("session.voice_idle_end_minutes", 30)))
    text_cut = datetime.now(UTC) - timedelta(hours=int(cfg("session.text_idle_end_hours", 12)))
    with db.tx() as conn:
        rows = db.all_(
            conn,
            """select id from sessions where status='active'
               and (%s::uuid is null or twin_id = %s::uuid)
               and ((modality='voice' and last_activity_at < %s)
                 or (modality='text' and last_activity_at < %s))""",
            (twin_id, twin_id, voice_cut, text_cut),
        )
    ended = []
    for r in rows:
        end_session(str(r["id"]), reason="idle_timeout")
        ended.append(str(r["id"]))
    return ended


def start(invite_token: str, modality: str, ip: str) -> dict:
    caps.check_ip(ip)
    if modality not in ("voice", "text"):
        raise HTTPException(400, "modality must be voice or text")
    with db.tx() as conn:
        inv = _invite(conn, invite_token)
    twin_id = str(inv["twin_id"])
    sweep_idle(twin_id)

    with db.tx() as conn:
        found = context.active_spec(conn, twin_id)
        if not found:
            raise HTTPException(409, "This interview isn't set up yet.")
        spec_id, spec = found
        if modality not in spec.modalities:
            raise HTTPException(400, f"{modality} isn't enabled for this interview.")
        # Multi-day text: an active, non-idle text session is resumed.
        if modality == "text":
            existing = db.one(conn, """select * from sessions where twin_id=%s and modality='text'
                                       and status='active' order by started_at desc limit 1""",
                              (twin_id,))
            if existing:
                return _session_payload(conn, existing, resumed=True)

    max_s = caps.start_session_caps(twin_id, modality, spec.session_prefs.max_minutes)
    with db.tx() as conn:
        first = db.one(conn, "select count(*) n from sessions where twin_id=%s", (twin_id,))["n"] == 0
        session = db.one(
            conn,
            """insert into sessions (twin_id, twin_spec_id, invite_id, modality, max_duration_s,
                                     stats)
               values (%s, %s, %s, %s, %s, %s) returning *""",
            (twin_id, spec_id, inv["id"], modality, max_s, db.j({"first_session": first})),
        )
    if modality == "text":
        _opening(session, spec, first)
    with db.tx() as conn:
        session = db.one(conn, "select * from sessions where id=%s", (session["id"],))
        return _session_payload(conn, session, resumed=False)


def _opening(session: dict, spec, first: bool) -> None:
    sid, twin_id = str(session["id"]), str(session["twin_id"])
    with db.tx() as conn:
        notes = context.notes_text(conn, twin_id)
        dnr = [r["topic"] for r in db.all_(conn, "select topic from do_not_raise where twin_id=%s",
                                           (twin_id,))]
    text = text_reply(spec, transcript="", notes=notes, do_not_raise=dnr, guidance="",
                      first_session=first, opening=True)
    with db.tx() as conn:
        conn.execute(
            """insert into turns (session_id, twin_id, idx, client_turn_id, speaker, text_live)
               values (%s, %s, 0, 'opening', 'interviewer', %s) on conflict do nothing""",
            (sid, twin_id, text),
        )
    context.invalidate_recent(sid)


def _session_payload(conn, session: dict, resumed: bool) -> dict:
    cfg = get_settings().cfg
    if session["modality"] == "voice":
        ttl = session["max_duration_s"] + int(cfg("session.token_grace_minutes", 30)) * 60
    else:
        ttl = 7 * 24 * 3600
    history = db.all_(conn, """select id, idx, speaker, text_live text from turns
                               where session_id=%s order by idx""", (session["id"],))
    return {
        "session_id": str(session["id"]),
        "session_token": sign_session(str(session["id"]), str(session["twin_id"]), ttl),
        "modality": session["modality"],
        "max_duration_s": session["max_duration_s"],
        "elapsed_s": context.elapsed_s(session),
        "resumed": resumed,
        "history": [{**h, "id": str(h["id"])} for h in history],
        "segment_seconds": cfg("recording.segment_seconds", 60),
        "overlap_seconds": cfg("recording.overlap_seconds", 2),
        "burst_idle_s": cfg("turn.text_burst_idle_s", 8),
    }


def end_session(session_id: str, *, reason: str = "user",
                expected_segments: int | None = None) -> dict:
    with db.tx() as conn:
        s = db.one(conn, "select * from sessions where id=%s for update", (session_id,))
        if not s:
            raise HTTPException(404, "Session not found.")
        if s["status"] != "active":
            return {"status": s["status"]}
        end_at = datetime.now(UTC)
        if reason == "idle_timeout":
            end_at = s["last_activity_at"]
        duration = max((end_at - s["started_at"]).total_seconds(), 0)
        conn.execute(
            """update sessions set status='ended', ended_at=%s, duration_s=%s, end_reason=%s,
               expected_segments=coalesce(%s, expected_segments) where id=%s""",
            (end_at, duration, reason, expected_segments, session_id),
        )
    if s["modality"] == "voice":
        init = float(get_settings().cfg("voice.init_billed_seconds", 15))
        caps.record_voice_usage(str(s["twin_id"]), max(duration, init))
    queue.enqueue("analyze_backlog", session_id, idem=f"backlog:{session_id}")
    return {"status": "ended", "duration_s": duration}


def touch(conn, session_id: str) -> None:
    conn.execute("update sessions set last_activity_at=now() where id=%s", (session_id,))
