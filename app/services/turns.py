"""Turn handling in two lanes (plan 6.4).

Fast lane `post_turn`: store the turn(s) and, for text, reply using the current tip.
Slow lane `analyze`: observer → signals → planner → one transaction → new tip.
"""

from __future__ import annotations

import re

from fastapi import HTTPException
from pydantic import BaseModel, Field

from app import db
from app.cache import redis as cache
from app.conversation import text_reply
from app.llm import llm
from app.models import Decision, ObserverOutput, TwinSpec
from app.observer import observe
from app.planner.planner import plan
from app.services import context
from app.services.sessions import touch
from app.settings import get_settings
from app.signals import baseline
from app.signals.tags import confident, make_tags
from app.signals.text_signals import text_features

WORD = re.compile(r"\w+", re.UNICODE)
STATUS_RANK = "case %s when 'done' then 2 when 'partial' then 1 else 0 end"
INTEREST_VAL = {"engaged": 1.0, "uncertain": 0.5, "tired": 0.3, "flat": 0.1, "avoidant": 0.0}


class TurnPart(BaseModel):
    text: str = Field(max_length=20000)
    start_ms: int | None = None
    end_ms: int | None = None


class TurnIn(BaseModel):
    client_turn_id: str = Field(min_length=4, max_length=64)
    subject: TurnPart
    interviewer: TurnPart | None = None
    features: dict[str, float] = Field(default_factory=dict)
    messages: list[dict] = Field(default_factory=list)


# ---- fast lane ------------------------------------------------------------------------------

def post_turn(session_id: str, body: TurnIn) -> dict:
    idem_key = cache.k_idem(f"{session_id}:{body.client_turn_id}")
    cached = cache.get_json(idem_key)
    if cached:
        return cached
    grace = int(get_settings().cfg("session.wrap_grace_seconds", 60))

    with cache.SessionLock(session_id):
        with db.tx() as conn:
            s = db.one(conn, "select * from sessions where id=%s for update", (session_id,))
            if not s or s["status"] != "active":
                raise HTTPException(409, "This chat has ended.")
            dup = db.one(conn, """select id from turns where session_id=%s and client_turn_id=%s
                                  and speaker='subject'""", (session_id, body.client_turn_id))
            if dup:
                return _replay(conn, s, str(dup["id"]), body.client_turn_id)
            elapsed = context.elapsed_s(s)
            if s["modality"] == "voice" and elapsed > s["max_duration_s"] + grace:
                raise HTTPException(409, "Time's up for this conversation.")
            idx = db.one(conn, "select coalesce(max(idx), -1) + 1 n from turns where session_id=%s",
                         (session_id,))["n"]
            if body.interviewer and body.interviewer.text.strip():
                conn.execute(
                    """insert into turns (session_id, twin_id, idx, client_turn_id, speaker,
                                          text_live, start_ms, end_ms)
                       values (%s,%s,%s,%s,'interviewer',%s,%s,%s)""",
                    (session_id, s["twin_id"], idx, body.client_turn_id, body.interviewer.text,
                     body.interviewer.start_ms, body.interviewer.end_ms),
                )
                idx += 1
            feats = dict(body.features)
            subject = db.one(
                conn,
                """insert into turns (session_id, twin_id, idx, client_turn_id, speaker, text_live,
                                      start_ms, end_ms, features)
                   values (%s,%s,%s,%s,'subject',%s,%s,%s,%s) returning id""",
                (session_id, s["twin_id"], idx, body.client_turn_id, body.subject.text,
                 body.subject.start_ms, body.subject.end_ms,
                 db.j({"browser": feats, "messages": body.messages})),
            )
            touch(conn, session_id)
            spec = context.spec_for_session(conn, s)
            state = context.load_state(conn, s, spec)
        context.invalidate_recent(session_id)

        reply = None
        if s["modality"] == "text":
            reply = _reply(s, spec, state.guidance, idx + 1, body.client_turn_id)

    resp = {
        "turn_id": str(subject["id"]),
        "reply": reply,
        "wrap_up": state.wrap_up or elapsed >= s["max_duration_s"],
        "elapsed_s": elapsed,
        "max_duration_s": s["max_duration_s"],
    }
    cache.set_json(idem_key, resp, int(get_settings().cfg("turn.idem_ttl_s", 86400)))
    return resp


def _reply(s: dict, spec, guidance: str, idx: int, cid: str) -> str:
    sid, twin_id = str(s["id"]), str(s["twin_id"])
    with db.tx() as conn:
        transcript = context.transcript(conn, sid)
        notes = context.notes_text(conn, twin_id)
        dnr = [r["topic"] for r in db.all_(conn, "select topic from do_not_raise where twin_id=%s",
                                           (twin_id,))]
    text = text_reply(spec, transcript=transcript, notes=notes, do_not_raise=dnr,
                      guidance=guidance, first_session=bool((s["stats"] or {}).get("first_session")))
    with db.tx() as conn:
        conn.execute(
            """insert into turns (session_id, twin_id, idx, client_turn_id, speaker, text_live)
               values (%s,%s,%s,%s,'interviewer',%s)""",
            (sid, twin_id, idx, f"{cid}:reply", text),
        )
    context.invalidate_recent(sid)
    return text


def _replay(conn, s: dict, turn_id: str, cid: str) -> dict:
    reply = db.one(conn, "select text_live from turns where session_id=%s and client_turn_id=%s",
                   (s["id"], f"{cid}:reply"))
    return {"turn_id": turn_id, "reply": reply["text_live"] if reply else None, "wrap_up": False,
            "elapsed_s": context.elapsed_s(s), "max_duration_s": s["max_duration_s"]}


# ---- slow lane ------------------------------------------------------------------------------

def analyze(session_id: str, turn_id: str) -> dict:
    """The session lock is held only to read inputs and to apply the result, never during the
    observer LLM call, so the fast lane never waits behind the thinking."""
    with cache.SessionLock(session_id):
        with db.tx() as conn:
            turn, s, spec, done = _load_turn(conn, session_id, turn_id)
            if done:
                return done
            twin_id = str(s["twin_id"])
            items = context.load_twin_state(conn, twin_id, spec).items
            notes = context.notes_text(conn, twin_id)
            transcript = context.transcript(conn, session_id, upto_idx=turn["idx"])

    obs, latency = observe(spec, transcript, notes, items)

    with cache.SessionLock(session_id):
        return _apply(session_id, turn_id, obs, latency)


def _load_turn(conn, session_id: str, turn_id: str) -> tuple[dict, dict, TwinSpec, dict | None]:
    """The turn, its session and spec, plus the cached result if it was already analyzed."""
    turn = db.one(conn, "select * from turns where id=%s and session_id=%s", (turn_id, session_id))
    if not turn or turn["speaker"] != "subject":
        raise HTTPException(404, "Turn not found.")
    s = db.one(conn, "select * from sessions where id=%s", (session_id,))
    spec = context.spec_for_session(conn, s)
    if turn["analyzed_at"]:
        snap = db.one(conn, "select decision from planner_snapshots where turn_id=%s "
                            "order by created_at desc limit 1", (turn_id,))
        return turn, s, spec, {"turn_id": turn_id, **(snap["decision"] if snap else {}),
                               "cached": True}
    return turn, s, spec, None


def _apply(session_id: str, turn_id: str, obs: ObserverOutput, latency: int) -> dict:
    # Everything is reloaded: other turns may have been saved or analyzed during the LLM call.
    with db.tx() as conn:
        turn, s, spec, done = _load_turn(conn, session_id, turn_id)
        if done:
            return done
        twin_id = str(s["twin_id"])
        state = context.load_state(conn, s, spec)
        ts = context.load_twin_state(conn, twin_id, spec)
        source = "text" if s["modality"] == "text" else "browser"
        base_row = db.one(conn, "select features from baselines where twin_id=%s and modality=%s "
                                "and source=%s", (twin_id, s["modality"], source))

    text = turn["text_live"] or ""
    stored = turn["features"] or {}
    if s["modality"] == "text":
        features = text_features(text, stored.get("messages"))
    else:
        features = {k: float(v) for k, v in (stored.get("browser") or {}).items()
                    if isinstance(v, int | float)}
    twin_stats = base_row["features"] if base_row else {}
    tags = make_tags(features, twin_stats, state.session_baseline)
    state.session_baseline = baseline.update(state.session_baseline, features)
    word_count = len(WORD.findall(text))

    _apply_observer(ts, obs)
    decision = plan(spec, state, ts, obs, confident(tags), word_count, turn["idx"])
    per_turn = state.engagement_trend[-1]
    state.last_analyzed_idx = max(state.last_analyzed_idx, turn["idx"])

    with db.tx() as conn:
        _persist(conn, s, turn, spec, obs, latency, features, tags, decision, per_turn,
                 word_count, source)
        conn.execute(
            """insert into planner_snapshots (twin_id, session_id, turn_id, state, decision)
               values (%s,%s,%s,%s,%s)""",
            (twin_id, session_id, turn_id, db.j(state.model_dump(mode="json")),
             db.j(decision.model_dump(mode="json"))),
        )
    context.save_state(state)
    return {"turn_id": turn_id, **decision.model_dump(mode="json")}


def _apply_observer(ts, obs: ObserverOutput) -> None:
    """Reflect this turn's slot updates in TwinState before planning."""
    for u in obs.slot_updates:
        if u.slot == "_items":
            ts.items.setdefault(u.domain_id, [])
            if u.item not in ts.items[u.domain_id]:
                ts.items[u.domain_id].append(u.item)
            continue
        key = (u.domain_id, u.item, u.slot)
        if ts.slots.get(key) != "done":
            ts.slots[key] = u.status
        if u.item and u.item not in ts.items.setdefault(u.domain_id, []):
            ts.items[u.domain_id].append(u.item)
    for t in obs.new_interests:
        ts.interests[t] = max(ts.interests.get(t, 0.0), 0.8)


def _persist(conn, s, turn, spec, obs: ObserverOutput, latency: int, features: dict, tags,
             decision: Decision, per_turn: str, word_count: int, source: str) -> None:
    sid, twin_id, tid = str(s["id"]), str(s["twin_id"]), str(turn["id"])
    model = get_settings().cfg("models.observer")
    conn.execute(
        """insert into observer_outputs (turn_id, output, model, latency_ms) values (%s,%s,%s,%s)
           on conflict (turn_id) do update set output=excluded.output, latency_ms=excluded.latency_ms""",
        (tid, db.j(obs.model_dump(mode="json")), model, latency),
    )
    tag_by_name = {t.tag: t for t in tags}
    for name, raw in features.items():
        t = tag_by_name.get(name)
        conn.execute(
            """insert into signals (session_id, turn_id, kind, source, value, confidence)
               values (%s,%s,%s,%s,%s,%s)""",
            (sid, tid, name, source, db.j({"raw": raw, "z": t.z if t else None}),
             t.confidence if t else None),
        )
    if "long_pause_before_answer" in tag_by_name:
        t = tag_by_name["long_pause_before_answer"]
        conn.execute("""insert into signals (session_id, turn_id, kind, source, value, confidence)
                        values (%s,%s,'long_pause_before_answer',%s,%s,%s)""",
                     (sid, tid, source, db.j({"raw": t.value}), t.confidence))
    for f in obs.facts:
        conn.execute("insert into notes (twin_id, turn_id, kind, text) values (%s,%s,%s,%s)",
                     (twin_id, tid, f.kind, f.text))
    for u in obs.slot_updates:
        status = "done" if u.slot == "_items" else u.status
        conn.execute(
            f"""insert into coverage_slots (twin_id, domain_id, item, slot, status, evidence)
                values (%s,%s,%s,%s,%s, array[%s]::uuid[])
                on conflict (twin_id, domain_id, item, slot) do update set
                  status = case
                    when {STATUS_RANK % 'excluded.status'} > {STATUS_RANK % 'coverage_slots.status'}
                    then excluded.status else coverage_slots.status end,
                  evidence = array_append(coverage_slots.evidence, %s::uuid),
                  updated_at = now()""",
            (twin_id, u.domain_id, u.item, u.slot, status, tid, tid),
        )
    val = INTEREST_VAL.get(per_turn, 0.5)
    for topic in dict.fromkeys(obs.topics + obs.new_interests):
        v = max(val, 0.8) if topic in obs.new_interests else val
        conn.execute(
            """insert into interests (twin_id, topic, score, n_obs, last_seen)
               values (%s,%s,%s,1,now())
               on conflict (twin_id, topic) do update set
                 score = (interests.score * interests.n_obs + excluded.score) / (interests.n_obs + 1),
                 n_obs = interests.n_obs + 1, last_seen = now()""",
            (twin_id, topic, v),
        )
    if decision.dropped_topic:
        conn.execute("""insert into do_not_raise (twin_id, topic, reason) values (%s,%s,'deflected')
                        on conflict do nothing""", (twin_id, decision.dropped_topic))
    conn.execute(
        """insert into register_coverage (twin_id, register, modality, word_count)
           values (%s,%s,%s,%s) on conflict (twin_id, register, modality)
           do update set word_count = register_coverage.word_count + excluded.word_count""",
        (twin_id, obs.talk_register, s["modality"], word_count),
    )
    conn.execute("update turns set register=%s, topics=%s, analyzed_at=now() where id=%s",
                 (obs.talk_register, obs.topics, tid))
    if s["modality"] == "text" and (turn["text_live"] or "").strip():
        conn.execute(
            """insert into corpus_items (twin_id, source, modality, turn_id, text, register)
               values (%s,'interview','text',%s,%s,%s) on conflict (turn_id) do nothing""",
            (twin_id, tid, turn["text_live"], obs.talk_register),
        )
    if features:
        row = db.one(conn, """select features from baselines where twin_id=%s and modality=%s
                              and source=%s for update""", (twin_id, s["modality"], source))
        stats = baseline.update(row["features"] if row else {}, features)
        conn.execute(
            """insert into baselines (twin_id, modality, source, features, n_turns)
               values (%s,%s,%s,%s,%s) on conflict (twin_id, modality, source) do update
               set features=excluded.features, n_turns=excluded.n_turns, updated_at=now()""",
            (twin_id, s["modality"], source, db.j(stats), baseline.n_turns(stats)),
        )
    conn.execute(
        """update sessions set stats = stats || jsonb_build_object(
             'analyzed', coalesce((stats->>'analyzed')::int, 0) + 1,
             'observer_ms_last', %s::int) where id=%s""",
        (latency, sid),
    )


def backlog(session_id: str) -> int:
    with db.tx() as conn:
        rows = db.all_(conn, """select id from turns where session_id=%s and speaker='subject'
                                and analyzed_at is null order by idx""", (session_id,))
    for r in rows:
        analyze(session_id, str(r["id"]))
    return len(rows)


# ---- delegation (plan 7.10) -----------------------------------------------------------------

def recall(twin_id: str, query: str, k: int = 3) -> list[str]:
    query = query.strip()
    if not query:
        return []
    with db.tx() as conn:
        has_vec = db.one(conn, "select 1 from notes where twin_id=%s and embedding is not null "
                               "and kind <> 'speech_habit' limit 1", (twin_id,))
        if has_vec:
            vec = llm().embed([query])[0]
            rows = db.all_(conn, """select text from notes where twin_id=%s and embedding is not null
                                    and kind <> 'speech_habit' order by embedding <=> %s::vector limit %s""",
                           (twin_id, str(vec), k))
            return [r["text"] for r in rows]
        rows = db.all_(conn, "select text from notes where twin_id=%s and kind <> 'speech_habit' "
                             "order by created_at desc limit 300", (twin_id,))
    q = set(WORD.findall(query.lower()))
    scored = sorted(((len(q & set(WORD.findall(r["text"].lower()))), r["text"]) for r in rows),
                    reverse=True)
    return [t for score, t in scored[:k] if score > 0]


def delegation(session_id: str, twin_id: str, last_subject_text: str) -> str:
    with db.tx() as conn:
        s = db.one(conn, "select * from sessions where id=%s", (session_id,))
        if not s or s["status"] != "active":
            raise HTTPException(409, "This chat has ended.")
        if context.elapsed_s(s) > s["max_duration_s"]:
            return "Time is up. Wrap up warmly now and say goodbye."
        spec = context.spec_for_session(conn, s)
        state = context.load_state(conn, s, spec)
    notes = recall(twin_id, last_subject_text)
    parts = []
    if notes:
        parts.append("From earlier conversations: " + " | ".join(notes))
    if state.guidance:
        parts.append("Coach's tip: " + state.guidance)
    return ("\n".join(parts) or "Nothing new. Keep following their lead.")[:1500]
