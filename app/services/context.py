"""Loading what a turn needs: spec, session state, twin state, transcript, notes.

Redis is a read-through cache everywhere here; every value can be rebuilt from Postgres.
"""

from __future__ import annotations

from datetime import UTC, datetime

from app import db
from app.cache import redis as cache
from app.models import SessionBudget, SessionState, TwinSpec
from app.planner.coverage import TwinState
from app.settings import get_settings

_spec_memo: dict[str, TwinSpec] = {}   # per-process memo, optimization only (plan 6.1)


def spec_for_session(conn, session: dict) -> TwinSpec:
    key = str(session["twin_spec_id"])
    if key in _spec_memo:
        return _spec_memo[key]
    row = db.one(conn, "select spec from twin_specs where id = %s", (key,))
    spec = TwinSpec.model_validate(row["spec"])
    _spec_memo[key] = spec
    return spec


def active_spec(conn, twin_id: str) -> tuple[str, TwinSpec] | None:
    cached = cache.get_json(cache.k_spec(twin_id))
    if cached:
        return cached["id"], TwinSpec.model_validate(cached["spec"])
    row = db.one(
        conn,
        """select id, spec from twin_specs where twin_id = %s and approved_at is not null
           order by version desc limit 1""",
        (twin_id,),
    )
    if not row:
        return None
    cache.set_json(cache.k_spec(twin_id), {"id": str(row["id"]), "spec": row["spec"]},
                   int(get_settings().cfg("redis.spec_ttl_s", 3600)))
    return str(row["id"]), TwinSpec.model_validate(row["spec"])


def elapsed_s(session: dict) -> float:
    return (datetime.now(UTC) - session["started_at"]).total_seconds()


def load_state(conn, session: dict, spec: TwinSpec) -> SessionState:
    sid = str(session["id"])
    raw = cache.get_json(cache.k_state(sid))
    if raw is None:
        snap = db.one(
            conn,
            "select state from planner_snapshots where session_id = %s order by created_at desc limit 1",
            (sid,),
        )
        raw = snap["state"] if snap else None
    if raw is None:
        state = SessionState(
            session_id=sid, twin_id=str(session["twin_id"]), modality=session["modality"],
            session_budget=SessionBudget(target_minutes=spec.session_prefs.target_minutes,
                                         max_duration_s=session["max_duration_s"]),
        )
    else:
        state = SessionState.model_validate(raw)
    state.session_budget.elapsed_s = elapsed_s(session)
    return state


def save_state(state: SessionState) -> None:
    cache.set_json(cache.k_state(state.session_id), state.model_dump(mode="json"),
                   int(get_settings().cfg("redis.state_ttl_s", 86400)))


def load_twin_state(conn, twin_id: str, spec: TwinSpec) -> TwinState:
    ts = TwinState()
    for r in db.all_(conn, "select domain_id, item, slot, status from coverage_slots where twin_id=%s",
                     (twin_id,)):
        if r["slot"] == "_items":
            ts.items.setdefault(r["domain_id"], []).append(r["item"])
        else:
            ts.slots[(r["domain_id"], r["item"], r["slot"])] = r["status"]
            if r["item"]:
                lst = ts.items.setdefault(r["domain_id"], [])
                if r["item"] not in lst:
                    lst.append(r["item"])
    ts.interests = {r["topic"]: r["score"] for r in db.all_(
        conn, "select topic, score from interests where twin_id=%s", (twin_id,))}
    ts.do_not_raise = {r["topic"] for r in db.all_(
        conn, "select topic from do_not_raise where twin_id=%s", (twin_id,))}
    ts.register_words = {}
    for r in db.all_(conn, "select register, sum(word_count) n from register_coverage "
                           "where twin_id=%s group by register", (twin_id,)):
        ts.register_words[r["register"]] = int(r["n"])
    asked = " ".join(r["text"].lower() for r in db.all_(
        conn, "select text from notes where twin_id=%s order by created_at desc limit 300",
        (twin_id,)))
    gaps = [r["text"] for r in db.all_(
        conn, "select text from notes where twin_id=%s and kind='followup' "
              "order by created_at desc limit 5", (twin_id,))]
    ts.seeds = [s for s in spec.creator_seeds if s.lower() not in asked] + gaps
    return ts


def notes_text(conn, twin_id: str, limit_chars: int | None = None) -> str:
    limit_chars = limit_chars or int(get_settings().cfg("session.notes_chars", 2000))
    rows = db.all_(conn, "select kind, text from notes where twin_id=%s and kind <> 'speech_habit' "
                         "order by created_at desc limit 80", (twin_id,))
    out, used = [], 0
    for r in rows:
        line = f"- {r['text']}"
        if used + len(line) > limit_chars:
            break
        out.append(line)
        used += len(line) + 1
    return "\n".join(reversed(out))


def label(speaker: str) -> str:
    return "Subject" if speaker == "subject" else "Interviewer"


def transcript(conn, session_id: str, upto_idx: int | None = None) -> str:
    """Last ~recent_chars of the session, oldest first. Rebuilt from Postgres when needed."""
    limit = int(get_settings().cfg("session.recent_chars", 5000))
    if upto_idx is None:
        cached = cache.get_json(cache.k_recent(session_id))
        if cached is not None:
            return cached
    params: tuple = (session_id,)
    where = "session_id=%s"
    if upto_idx is not None:
        where += " and idx <= %s"
        params = (session_id, upto_idx)
    rows = db.all_(conn, f"select speaker, coalesce(text_corpus, text_live) t from turns "
                         f"where {where} order by idx desc limit 80", params)
    parts, used = [], 0
    for r in rows:
        line = f"{label(r['speaker'])}: {r['t'] or ''}"
        if used + len(line) > limit and parts:
            break
        parts.append(line)
        used += len(line)
    text = "\n".join(reversed(parts))[-limit:]
    if upto_idx is None:
        cache.set_json(cache.k_recent(session_id), text,
                       int(get_settings().cfg("redis.state_ttl_s", 86400)))
    return text


def invalidate_recent(session_id: str) -> None:
    cache.delete(cache.k_recent(session_id))
