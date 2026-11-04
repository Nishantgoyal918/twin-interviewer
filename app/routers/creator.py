"""Creator API: twins, specs, invites, coverage, debug view, labels, imports."""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app import db, queue, twin
from app.brief import compile_brief, normalize
from app.cache import redis as cache
from app.models import TwinSpec
from app.security.auth import creator_id, own_twin
from app.security.tokens import new_invite_token
from app.settings import get_settings
from app.storage import storage

router = APIRouter(prefix="/api")


class BriefIn(BaseModel):
    brief: str = Field(min_length=10, max_length=4000)


@router.get("/public-config")
def public_config() -> dict:
    s = get_settings()
    return {"supabase_url": s.supabase_url, "supabase_anon_key": s.supabase_anon_key,
            "dev_auth": s.dev_auth, "open_access": s.open_access}


@router.get("/twins")
def list_twins(uid: str = Depends(creator_id)) -> list[dict]:
    with db.tx() as conn:
        return db.all_(conn, """select t.id, t.subject_name, t.status, t.simulated, t.created_at,
                  (select max(version) from twin_specs s where s.twin_id=t.id) latest_version,
                  (select count(*) from sessions x where x.twin_id=t.id) sessions
                  from twins t where creator_id=%s order by created_at desc""", (uid,))


@router.post("/twins")
def create_twin(body: BriefIn, uid: str = Depends(creator_id)) -> dict:
    spec = compile_brief(body.brief)
    with db.tx() as conn:
        t = db.one(conn, "insert into twins (creator_id, subject_name) values (%s,%s) returning *",
                   (uid, spec.subject.name))
        conn.execute("""insert into twin_specs (twin_id, version, spec, brief_text)
                        values (%s, 1, %s, %s)""",
                     (t["id"], db.j(spec.model_dump(mode="json")), body.brief))
    return {"twin_id": str(t["id"]), "version": 1, "spec": spec.model_dump(mode="json")}


@router.get("/twins/{twin_id}/spec")
def get_spec(twin_id: str, uid: str = Depends(creator_id)) -> dict:
    with db.tx() as conn:
        t = own_twin(conn, twin_id, uid)
        rows = db.all_(conn, """select version, spec, brief_text, approved_at, created_at
                                from twin_specs where twin_id=%s order by version desc""", (twin_id,))
    return {"twin": t, "latest": rows[0] if rows else None,
            "versions": [{k: r[k] for k in ("version", "approved_at", "created_at")} for r in rows]}


class SpecIn(BaseModel):
    spec: dict


@router.put("/twins/{twin_id}/spec")
def put_spec(twin_id: str, body: SpecIn, uid: str = Depends(creator_id)) -> dict:
    try:
        spec = normalize(TwinSpec.model_validate(body.spec))
    except Exception as exc:
        raise HTTPException(422, f"The spec isn't valid: {exc}") from exc
    with db.tx() as conn:
        own_twin(conn, twin_id, uid)
        v = db.one(conn, "select coalesce(max(version),0)+1 v from twin_specs where twin_id=%s",
                   (twin_id,))["v"]
        conn.execute("insert into twin_specs (twin_id, version, spec) values (%s,%s,%s)",
                     (twin_id, v, db.j(spec.model_dump(mode="json"))))
        conn.execute("update twins set subject_name=%s where id=%s", (spec.subject.name, twin_id))
    return {"version": v, "spec": spec.model_dump(mode="json")}


@router.post("/twins/{twin_id}/spec/approve")
def approve_spec(twin_id: str, uid: str = Depends(creator_id)) -> dict:
    with db.tx() as conn:
        own_twin(conn, twin_id, uid)
        row = db.one(conn, """select id, version, spec from twin_specs where twin_id=%s
                              order by version desc limit 1""", (twin_id,))
        if not row:
            raise HTTPException(404, "No spec to approve.")
        conn.execute("update twin_specs set approved_at=now() where id=%s", (row["id"],))
        conn.execute("update twins set status='active' where id=%s", (twin_id,))
        for topic in row["spec"].get("sensitivities", []):
            conn.execute("""insert into do_not_raise (twin_id, topic, reason) values (%s,%s,
                            'creator sensitivity') on conflict do nothing""", (twin_id, topic))
    cache.delete(cache.k_spec(twin_id))
    return {"approved_version": row["version"], "status": "active"}


class InviteIn(BaseModel):
    label: str | None = None
    expires_days: int | None = Field(default=30, ge=1, le=365)


@router.post("/twins/{twin_id}/invites")
def create_invite(twin_id: str, body: InviteIn, uid: str = Depends(creator_id)) -> dict:
    token, token_hash = new_invite_token()
    expires = datetime.now(UTC) + timedelta(days=body.expires_days) if body.expires_days else None
    with db.tx() as conn:
        t = own_twin(conn, twin_id, uid)
        if t["status"] != "active":
            raise HTTPException(409, "Approve the spec before inviting anyone.")
        row = db.one(conn, """insert into subject_invites (twin_id, token_hash, label, expires_at)
                              values (%s,%s,%s,%s) returning id""",
                     (twin_id, token_hash, body.label, expires))
    return {"invite_id": str(row["id"]), "link": f"{get_settings().app_base_url}/s/{token}",
            "expires_at": expires}


@router.get("/twins/{twin_id}/invites")
def list_invites(twin_id: str, uid: str = Depends(creator_id)) -> list[dict]:
    with db.tx() as conn:
        own_twin(conn, twin_id, uid)
        return db.all_(conn, """select id, label, expires_at, revoked_at, created_at
                                from subject_invites where twin_id=%s order by created_at desc""",
                       (twin_id,))


@router.post("/twins/{twin_id}/invites/{invite_id}/revoke")
def revoke_invite(twin_id: str, invite_id: str, uid: str = Depends(creator_id)) -> dict:
    with db.tx() as conn:
        own_twin(conn, twin_id, uid)
        conn.execute("update subject_invites set revoked_at=now() where id=%s and twin_id=%s",
                     (invite_id, twin_id))
    return {"revoked": True}


@router.get("/twins/{twin_id}/coverage")
def coverage(twin_id: str, uid: str = Depends(creator_id)) -> dict:
    with db.tx() as conn:
        own_twin(conn, twin_id, uid)
        q = lambda sql: db.all_(conn, sql, (twin_id,))  # noqa: E731
        return {
            "slots": q("""select domain_id, item, slot, status, cardinality(evidence) n_evidence
                          from coverage_slots where twin_id=%s order by domain_id, item, slot"""),
            "registers": q("""select register, modality, word_count, done,
                              jsonb_array_length(feature_history) sessions
                              from register_coverage where twin_id=%s order by register"""),
            "interests": q("""select topic, round(score::numeric, 2) score, n_obs from interests
                              where twin_id=%s order by score desc limit 30"""),
            "do_not_raise": q("select topic, reason from do_not_raise where twin_id=%s"),
            "notes": q("""select kind, text, created_at from notes where twin_id=%s
                          order by created_at desc limit 50"""),
            "probe_runs": q("""select result, created_at from eval_runs where twin_id=%s
                               and kind='probe_test' order by created_at desc limit 5"""),
        }


@router.get("/twins/{twin_id}/sessions")
def sessions(twin_id: str, uid: str = Depends(creator_id)) -> list[dict]:
    with db.tx() as conn:
        own_twin(conn, twin_id, uid)
        return db.all_(conn, """select s.id, s.modality, s.status, s.end_reason, s.started_at,
                  s.duration_s, s.stats,
                  (select count(*) from turns t where t.session_id=s.id and t.speaker='subject') turns
                  from sessions s where twin_id=%s order by started_at desc""", (twin_id,))


@router.get("/twins/{twin_id}/sessions/{session_id}/recording")
def recording(twin_id: str, session_id: str, uid: str = Depends(creator_id)) -> dict:
    """The whole call to listen to: the interviewer on the left, them on the right. Older calls
    only kept their microphone (for transcription); simulator runs keep their own recording.
    `swap`: an early simulator recording with the channels the other way round."""
    with db.tx() as conn:
        own_twin(conn, twin_id, uid)
        sess = db.one(conn, "select stats from sessions where id=%s and twin_id=%s",
                      (session_id, twin_id))
        if not sess:
            raise HTTPException(404, "Session not found.")
        parts = db.all_(conn, """select seq, start_ms, end_ms, storage_path from audio_assets
                                 where session_id=%s and kind='call' order by seq""", (session_id,))
    store = storage()
    if parts:
        return {"source": "call", "parts": [
            {"url": store.signed_read_url(p["storage_path"], 6 * 3600),
             "start_ms": p["start_ms"], "end_ms": p["end_ms"]} for p in parts]}
    if (sess["stats"] or {}).get("sim_persona") and get_settings().storage_backend != "local":
        try:
            return {"source": "simulator",
                    "swap": sess["stats"].get("rec_layout") != "interviewer_left",
                    "parts": [{"url": store.signed_read_url(f"sim/{session_id}.webm", 6 * 3600)}]}
        except Exception:  # noqa: BLE001 - no recording was uploaded
            pass
    return {"source": None, "parts": []}


@router.get("/twins/{twin_id}/sessions/{session_id}/debug")
def debug(twin_id: str, session_id: str, uid: str = Depends(creator_id)) -> dict:
    with db.tx() as conn:
        own_twin(conn, twin_id, uid)
        turns = db.all_(conn, """select t.id, t.idx, t.speaker, t.text_live, t.text_corpus,
                  t.register, t.topics, t.start_ms, t.end_ms, o.output observer, o.latency_ms,
                  l.label, (select decision from planner_snapshots p where p.turn_id=t.id
                            order by created_at desc limit 1) decision
                  from turns t left join observer_outputs o on o.turn_id=t.id
                  left join labels l on l.turn_id=t.id
                  where t.session_id=%s and t.twin_id=%s order by t.idx""", (session_id, twin_id))
        sig = db.all_(conn, """select turn_id, kind, source, value, confidence from signals
                               where session_id=%s""", (session_id,))
        sess = db.one(conn, "select * from sessions where id=%s", (session_id,))
    by_turn: dict[str, list] = {}
    for r in sig:
        by_turn.setdefault(str(r["turn_id"]), []).append(r)
    subj = sum(len((t["text_corpus"] or t["text_live"] or "").split())
               for t in turns if t["speaker"] == "subject")
    total = subj + sum(len((t["text_live"] or "").split())
                       for t in turns if t["speaker"] == "interviewer")
    for t in turns:
        t["signals"] = by_turn.get(str(t["id"]), [])
    return {"session": sess, "talk_share": round(subj / total, 3) if total else None,
            "turns": turns}


class LabelIn(BaseModel):
    turn_id: str
    label: str = Field(pattern="^(engaged|flat|hesitant|laughing|unclear)$")


@router.post("/labels")
def label(body: LabelIn, uid: str = Depends(creator_id)) -> dict:
    with db.tx() as conn:
        t = db.one(conn, "select twin_id from turns where id=%s", (body.turn_id,))
        if not t:
            raise HTTPException(404, "Turn not found.")
        own_twin(conn, str(t["twin_id"]), uid)
        conn.execute("""insert into labels (turn_id, label, labeled_by) values (%s,%s,%s)
                        on conflict (turn_id) do update set label=excluded.label""",
                     (body.turn_id, body.label, uid))
    return {"ok": True}


class ImportIn(BaseModel):
    kind: str = Field(pattern="^(whatsapp|email|doc|other)$")
    filename: str
    author_name: str | None = None


@router.post("/twins/{twin_id}/imports")
def create_import(twin_id: str, body: ImportIn, uid: str = Depends(creator_id)) -> dict:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", body.filename)[-80:]
    path = f"imports/{twin_id}/{uuid.uuid4().hex}-{safe}"
    with db.tx() as conn:
        own_twin(conn, twin_id, uid)
        row = db.one(conn, """insert into imports (twin_id, kind, author_name, storage_path)
                              values (%s,%s,%s,%s) returning id""",
                     (twin_id, body.kind, body.author_name, path))
    return {"import_id": str(row["id"]), "upload": storage().signed_upload(path, "text/plain")}


@router.post("/twins/{twin_id}/imports/{import_id}/complete")
def complete_import(twin_id: str, import_id: str, uid: str = Depends(creator_id)) -> dict:
    with db.tx() as conn:
        own_twin(conn, twin_id, uid)
    job = queue.enqueue("import_ingest", import_id, idem=f"import:{import_id}")
    return {"job_id": job}


class AskIn(BaseModel):
    question: str = Field(min_length=2, max_length=1000)


@router.post("/twins/{twin_id}/ask")
def ask_twin(twin_id: str, body: AskIn, uid: str = Depends(creator_id)) -> dict:
    with db.tx() as conn:
        own_twin(conn, twin_id, uid)
    return {"answer": twin.answer(twin_id, body.question)}
