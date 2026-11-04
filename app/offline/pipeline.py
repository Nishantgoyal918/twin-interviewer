"""Job handlers. Each step is small and enqueues the next (plan 8.3).

voice: analyze_backlog → transcribe_session → (Deepgram webhooks, fan-in) → align_transcript
       → timing_features → coverage_update
text:  analyze_backlog → coverage_update
"""

from __future__ import annotations

import json
import logging

from app import db, queue
from app.llm import llm
from app.models import TwinSpec
from app.offline import deepgram
from app.offline.align import align, merge_segments
from app.offline.imports import parse_plain, parse_whatsapp
from app.offline.style import habit_line, is_stable, style_stats, voice_profile
from app.services import turns
from app.settings import get_settings
from app.signals import baseline
from app.signals.timing_features import timing_features
from app.storage import storage
from app.twin import probe_domain, qa_pairs

log = logging.getLogger("ti.pipeline")


def _session(conn, sid: str) -> dict:
    return db.one(conn, "select * from sessions where id=%s", (sid,))


@queue.handler("analyze_backlog")
def analyze_backlog(job: dict) -> None:
    sid = str(job["ref_id"])
    turns.backlog(sid)
    with db.tx() as conn:
        s = _session(conn, sid)
        conn.execute("update sessions set status='processing' where id=%s and status='ended'", (sid,))
    if s["modality"] == "voice":
        queue.enqueue("transcribe_session", sid, {"recheck": 0}, idem=f"transcribe:{sid}:0")
    else:
        queue.enqueue("coverage_update", sid, idem=f"coverage:{sid}")


@queue.handler("transcribe_session")
def transcribe_session(job: dict) -> None:
    sid = str(job["ref_id"])
    cfg = get_settings().cfg
    recheck = int((job["payload"] or {}).get("recheck", 0))
    with db.tx() as conn:
        s = _session(conn, sid)
        segs = db.all_(conn, """select * from audio_assets where session_id=%s and kind='segment'
                                order by seq""", (sid,))
    expected = s["expected_segments"] or 0
    if len(segs) < expected and recheck < int(cfg("recording.transcribe_recheck_max", 6)):
        queue.enqueue("transcribe_session", sid, {"recheck": recheck + 1},
                      idem=f"transcribe:{sid}:{recheck + 1}",
                      delay_s=int(cfg("recording.transcribe_recheck_delay_s", 300)))
        return
    if not segs or not get_settings().deepgram_api_key:
        if segs:
            log.warning("DEEPGRAM_API_KEY missing: skipping transcription for %s", sid)
        queue.enqueue("coverage_update", sid, idem=f"coverage:{sid}")
        return
    base = get_settings().app_base_url
    secret = get_settings().deepgram_webhook_secret
    languages = _languages(sid)
    local = _is_local(base)
    for seg in segs:
        if seg["transcript_status"] in ("submitted", "done"):
            continue
        if local:
            # Deepgram can't reach localhost: transcribe now and take the callback path directly.
            audio = storage().get_bytes(seg["storage_path"])
            deepgram_callback(str(seg["id"]), deepgram.transcribe_now(
                audio, seg.get("mime") or "audio/webm", deepgram.params(languages)))
            continue
        url = storage().signed_read_url(seg["storage_path"], 6 * 3600)
        deepgram.submit(url, f"{base}/api/webhooks/deepgram?asset={seg['id']}&secret={secret}",
                        languages)
        with db.tx() as conn:
            conn.execute("update audio_assets set transcript_status='submitted' where id=%s",
                         (seg["id"],))


def deepgram_callback(asset_id: str, body: dict) -> None:
    """Webhook body → shifted words in Storage; the last segment triggers alignment."""
    with db.tx() as conn:
        seg = db.one(conn, "select * from audio_assets where id=%s for update", (asset_id,))
        if seg is None or seg["transcript_status"] == "done":
            return
    words = deepgram.words_from_response(body, offset_ms=seg["start_ms"])
    sid = str(seg["session_id"])
    raw_path = f"transcripts/{sid}/{seg['seq']:04d}.raw.json"
    words_path = f"transcripts/{sid}/{seg['seq']:04d}.words.json"
    storage().put_bytes(raw_path, json.dumps(body).encode(), "application/json")
    storage().put_bytes(words_path, json.dumps(words).encode(), "application/json")
    with db.tx() as conn:
        conn.execute("""update audio_assets set transcript_status='done', transcript_path=%s
                        where id=%s""", (words_path, asset_id))
        pending = db.one(conn, """select count(*) n from audio_assets where session_id=%s
                  and kind='segment' and transcript_status in ('pending','submitted')""", (sid,))["n"]
    if pending == 0:
        queue.enqueue("align_transcript", sid, idem=f"align:{sid}")


def _is_local(base_url: str) -> bool:
    return any(h in base_url for h in ("://localhost", "://127.0.0.1", "://testserver"))


def _languages(sid: str) -> list[str]:
    from app.services.context import spec_for_session

    with db.tx() as conn:
        return list(spec_for_session(conn, _session(conn, sid)).subject.languages)


@queue.handler("align_transcript")
def align_transcript(job: dict) -> None:
    sid = str(job["ref_id"])
    with db.tx() as conn:
        segs = db.all_(conn, """select * from audio_assets where session_id=%s and kind='segment'
                                and transcript_status='done' order by seq""", (sid,))
        trows = db.all_(conn, """select id, speaker, start_ms, end_ms, register, twin_id, text_live as text
                                 from turns
                                 where session_id=%s order by idx""", (sid,))
    segments = [{"start_ms": s["start_ms"], "end_ms": s["end_ms"],
                 "words": json.loads(storage().get_bytes(s["transcript_path"]))} for s in segs]
    by_turn = align(merge_segments(segments), trows)
    regs = {str(t["id"]): t for t in trows}
    with db.tx() as conn:
        for tid, words in by_turn.items():
            if not words:
                continue
            text = " ".join(w[0] for w in words)
            conn.execute("update turns set text_corpus=%s, words=%s where id=%s",
                         (text, db.j(words), tid))
            t = regs[tid]
            conn.execute(
                """insert into corpus_items (twin_id, source, modality, turn_id, text, register)
                   values (%s,'interview','voice',%s,%s,%s)
                   on conflict (turn_id) do update set text=excluded.text""",
                (t["twin_id"], tid, text, t["register"]),
            )
    queue.enqueue("timing_features", sid, idem=f"timing:{sid}")


@queue.handler("timing_features")
def timing_features_job(job: dict) -> None:
    sid = str(job["ref_id"])
    with db.tx() as conn:
        s = _session(conn, sid)
        rows = db.all_(conn, """select id, words from turns where session_id=%s
                                and speaker='subject' and words is not null order by idx""", (sid,))
        row = db.one(conn, """select features from baselines where twin_id=%s and modality='voice'
                              and source='deepgram_timing' for update""", (s["twin_id"],))
        stats = row["features"] if row else {}
        reliable = deepgram.single_language(_languages(sid)) is not None
        for r in rows:
            feats = timing_features(r["words"], fillers_reliable=reliable)
            for name, val in feats.items():
                z = baseline.zscore(stats.get(name), val)
                conn.execute("""insert into signals (session_id, turn_id, kind, source, value)
                                values (%s,%s,%s,'deepgram_timing',%s)""",
                             (sid, r["id"], name, db.j({"raw": val, "z": z})))
            stats = baseline.update(stats, feats)
        conn.execute(
            """insert into baselines (twin_id, modality, source, features, n_turns)
               values (%s,'voice','deepgram_timing',%s,%s) on conflict (twin_id, modality, source)
               do update set features=excluded.features, n_turns=excluded.n_turns, updated_at=now()""",
            (s["twin_id"], db.j(stats), baseline.n_turns(stats)),
        )
    queue.enqueue("coverage_update", sid, idem=f"coverage:{sid}")


@queue.handler("coverage_update")
def coverage_update(job: dict) -> None:
    sid = str(job["ref_id"])
    cfg = get_settings().cfg
    with db.tx() as conn:
        s = _session(conn, sid)
        twin_id = str(s["twin_id"])
        spec = TwinSpec.model_validate(db.one(conn, "select spec from twin_specs where id=%s",
                                              (s["twin_spec_id"],))["spec"])
        rows = db.all_(conn, """select register, coalesce(text_corpus, text_live) t from turns
                                where session_id=%s and speaker='subject' and register is not null""",
                       (sid,))
        by_reg: dict[str, list[str]] = {}
        for r in rows:
            by_reg.setdefault(r["register"], []).append(r["t"] or "")
        min_words = int(cfg("coverage.register_min_words", 2000))
        k = int(cfg("coverage.stability_sessions", 3))
        tol = float(cfg("coverage.stability_tolerance", 0.15))
        for reg, texts in by_reg.items():
            stats = style_stats(texts)
            rc = db.one(conn, """select * from register_coverage where twin_id=%s and register=%s
                                 and modality=%s for update""", (twin_id, reg, s["modality"]))
            if rc is None:
                continue
            history = [h for h in rc["feature_history"] if h.get("session_id") != sid]
            history.append({"session_id": sid, "stats": stats,
                            "words": sum(len(t.split()) for t in texts)})
            done = rc["word_count"] >= min_words and is_stable(history, k, tol)
            conn.execute("""update register_coverage set feature_history=%s, done=%s
                            where twin_id=%s and register=%s and modality=%s""",
                         (db.j(history[-20:]), done, twin_id, reg, s["modality"]))
        touched = db.all_(conn, """select distinct c.domain_id, c.item from coverage_slots c
                   join turns t on t.id = any(c.evidence) where t.session_id=%s""", (sid,))
    _speech_habits(twin_id)
    _embed_missing(twin_id)
    _probe(twin_id, spec, sid, touched)
    with db.tx() as conn:
        conn.execute("update sessions set status='processed' where id=%s", (sid,))


def _speech_habits(twin_id: str) -> None:
    """Re-derive, from everything they've said, their signature phrases ('speech_habit' notes)
    and the exchanges that best show their voice (voice_examples). Both are for the twin only:
    the interviewer must not copy them."""
    with db.tx() as conn:
        pairs = qa_pairs(conn, twin_id)
    habits, examples = voice_profile(pairs)
    with db.tx() as conn:
        conn.execute("delete from notes where twin_id=%s and kind='speech_habit'", (twin_id,))
        for h in habits:
            conn.execute("insert into notes (twin_id, kind, text) values (%s,'speech_habit',%s)",
                         (twin_id, habit_line(h)))
        if examples:  # keep the old ones if this pass found none
            conn.execute("delete from voice_examples where twin_id=%s", (twin_id,))
        for e in examples:
            conn.execute("""insert into voice_examples (twin_id, turn_id, question, answer, why)
                            values (%s,%s,%s,%s,%s)""",
                         (twin_id, e["turn_id"], e["question"], e["answer"], e["why"]))


def _embed_missing(twin_id: str) -> None:
    for table in ("corpus_items", "notes"):
        while True:
            with db.tx() as conn:
                rows = db.all_(conn, f"select id, text from {table} where twin_id=%s and "
                                     f"embedding is null limit 64", (twin_id,))
            if not rows:
                break
            vecs = llm().embed([r["text"][:6000] for r in rows])
            with db.tx() as conn:
                for r, v in zip(rows, vecs, strict=True):
                    conn.execute(f"update {table} set embedding=%s::vector where id=%s",
                                 (str(v), r["id"]))


def _probe(twin_id: str, spec: TwinSpec, sid: str, touched: list[dict]) -> None:
    limit = int(get_settings().cfg("coverage.probes_per_session", 5))
    if limit <= 0 or not touched:
        return
    by_domain: dict[str, list[str]] = {}
    for t in touched:
        by_domain.setdefault(t["domain_id"], [])
        if t["item"]:
            by_domain[t["domain_id"]].append(t["item"])
    results = []
    with db.tx() as conn:
        for domain_id, items in by_domain.items():
            for v in probe_domain(conn, twin_id, spec, domain_id, items, limit):
                results.append(v.model_dump())
                if v.answerable:
                    conn.execute("""update coverage_slots set status='done', updated_at=now()
                                    where twin_id=%s and domain_id=%s and item=%s and slot=%s""",
                                 (twin_id, domain_id, v.item if v.item != "''" else "", v.slot))
        if results:
            conn.execute("insert into eval_runs (twin_id, kind, result) values (%s,'probe_test',%s)",
                         (twin_id, db.j({"session_id": sid, "verdicts": results})))


@queue.handler("import_ingest")
def import_ingest(job: dict) -> None:
    import_id = str(job["ref_id"])
    with db.tx() as conn:
        imp = db.one(conn, "select * from imports where id=%s", (import_id,))
        spec_row = db.one(conn, """select spec from twin_specs where twin_id=%s
                                   order by version desc limit 1""", (imp["twin_id"],))
    raw = storage().get_bytes(imp["storage_path"]).decode("utf-8", errors="replace")
    author = imp["author_name"] or TwinSpec.model_validate(spec_row["spec"]).subject.name
    texts = parse_whatsapp(raw, author) if imp["kind"] == "whatsapp" else parse_plain(raw)
    with db.tx() as conn:
        for t in texts:
            conn.execute("""insert into corpus_items (twin_id, source, modality, import_id, text)
                            values (%s,'import','text',%s,%s)""", (imp["twin_id"], import_id, t))
        conn.execute("update imports set status=%s where id=%s",
                     ("done" if texts else "empty", import_id))
    _embed_missing(str(imp["twin_id"]))
