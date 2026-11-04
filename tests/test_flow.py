"""End-to-end flows through the HTTP API against a real Postgres (plan phase acceptance)."""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime, timedelta

from app import db
from app.cache import redis as cache
from tests.conftest import CREATOR


def start(client, twin, modality="text"):
    r = client.post("/api/sessions", json={"invite_token": twin["token"], "modality": modality})
    assert r.status_code == 200, r.text
    return r.json()


def auth(s):
    return {"authorization": f"Bearer {s['session_token']}"}


def say(client, s, text, cid, interviewer=None, features=None):
    body = {"client_turn_id": cid, "subject": {"text": text}, "features": features or {}}
    if interviewer:
        body["interviewer"] = interviewer
    r = client.post("/api/turn", json=body, headers=auth(s))
    assert r.status_code == 200, r.text
    return r.json()


def test_creator_flow_and_invite(client, twin):
    spec = client.get(f"/api/twins/{twin['id']}/spec", headers=CREATOR).json()
    assert spec["twin"]["status"] == "active" and spec["latest"]["approved_at"]
    info = client.get(f"/api/invites/{twin['token']}").json()
    assert info["subject_name"] == "Mother"
    assert client.get("/api/invites/not-a-token").status_code == 404
    # other creators can't see it
    other = {"x-dev-creator": "22222222-2222-2222-2222-222222222222"}
    assert client.get(f"/api/twins/{twin['id']}/spec", headers=other).status_code == 404


def test_text_session_two_lanes(client, twin, fake_llm):
    s = start(client, twin)
    assert s["history"][0]["speaker"] == "interviewer"          # opening line
    fake_llm.on("text_agent", lambda ins, inp: "What did she add last?")
    r = say(client, s, "First you roast the jeera until it smells like toast, then the ghee.", "c-001")
    assert r["reply"] == "What did she add last?"
    a = client.post(f"/api/turn/{r['turn_id']}/analyze", headers=auth(s)).json()
    assert a["guidance"] and a["engagement"]
    # analyze is idempotent
    again = client.post(f"/api/turn/{r['turn_id']}/analyze", headers=auth(s)).json()
    assert again.get("cached") is True
    # retried post with the same client_turn_id returns the same reply, no new rows
    assert say(client, s, "ignored", "c-001")["turn_id"] == r["turn_id"]
    with db.tx() as conn:
        n = db.one(conn, "select count(*) n from turns where session_id=%s", (s["session_id"],))["n"]
        corpus = db.one(conn, "select count(*) n from corpus_items")["n"]
    assert n == 3 and corpus == 1


def test_turn_does_not_wait_for_analysis(client, twin, fake_llm):
    from app.fakes import fake_json

    s = start(client, twin)
    first = say(client, s, "My grandmother kept chickens behind the house in Ajmer.", "w-001")
    in_llm, release = threading.Event(), threading.Event()

    def slow_observer(ins, inp):
        in_llm.set()
        assert release.wait(10)
        return fake_json("observer", inp)

    fake_llm.on("observer", slow_observer)
    out = {}
    worker = threading.Thread(target=lambda: out.update(
        r=client.post(f"/api/turn/{first['turn_id']}/analyze", headers=auth(s))))
    worker.start()
    try:
        assert in_llm.wait(10)
        t0 = time.monotonic()
        second = say(client, s, "Every morning I collected the eggs before school.", "w-002")
        assert time.monotonic() - t0 < 2                        # not stuck behind the lock
        assert "r" not in out                                   # analysis still thinking
    finally:
        release.set()
        worker.join(10)
    a = out["r"]
    assert a.status_code == 200, a.text
    assert a.json()["guidance"]
    with db.tx() as conn:
        snaps = db.all_(conn, "select turn_id, state from planner_snapshots where session_id=%s",
                        (s["session_id"],))
        turns = {str(r["id"]): r["analyzed_at"] for r in db.all_(
            conn, "select id, analyzed_at from turns where session_id=%s and speaker='subject'",
            (s["session_id"],))}
    assert [str(x["turn_id"]) for x in snaps] == [first["turn_id"]]
    assert snaps[0]["state"]["last_analyzed_idx"] == 1
    assert turns[first["turn_id"]] and not turns[second["turn_id"]]
    # the second turn analyzes on top of the first's state
    b = client.post(f"/api/turn/{second['turn_id']}/analyze", headers=auth(s)).json()
    assert b["guidance"] and "cached" not in b
    state = cache.get_json(cache.k_state(s["session_id"]))
    assert state["last_analyzed_idx"] == 3 and len(state["engagement_trend"]) == 2


def test_concurrent_analyze_is_idempotent(client, twin, fake_llm):
    from app.fakes import fake_json

    s = start(client, twin)
    r = say(client, s, "We used to fly kites from the roof every January.", "i-001")
    both_in, n = threading.Barrier(2, timeout=10), []

    def observer(ins, inp):
        n.append(1)
        both_in.wait()                                          # both analyses are mid-LLM
        return fake_json("observer", inp)

    fake_llm.on("observer", observer)
    outs = []
    workers = [threading.Thread(target=lambda: outs.append(
        client.post(f"/api/turn/{r['turn_id']}/analyze", headers=auth(s)).json()))
        for _ in range(2)]
    for w in workers:
        w.start()
    for w in workers:
        w.join(15)
    assert len(n) == 2 and len(outs) == 2
    assert sorted(bool(o.get("cached")) for o in outs) == [False, True]
    with db.tx() as conn:
        snaps = db.one(conn, "select count(*) n from planner_snapshots where turn_id=%s",
                       (r["turn_id"],))["n"]
        outputs = db.one(conn, "select count(*) n from observer_outputs where turn_id=%s",
                         (r["turn_id"],))["n"]
    assert snaps == 1 and outputs == 1


def test_idempotency_survives_redis_loss(client, twin):
    s = start(client, twin)
    r1 = say(client, s, "hello there friend", "dup-1")
    cache.set_backend(cache.MemoryBackend())                    # Redis wiped
    r2 = say(client, s, "hello there friend", "dup-1")
    assert r1["turn_id"] == r2["turn_id"]


def test_killing_redis_mid_session_keeps_working(client, twin):
    s = start(client, twin)
    r = say(client, s, "We lived in Jaipur when I was small, near the old market.", "k-001")
    client.post(f"/api/turn/{r['turn_id']}/analyze", headers=auth(s))
    cache.set_backend(cache.MemoryBackend())                    # state evicted
    r = say(client, s, "My aunt Kamla taught me to cook there, every Sunday.", "k-002")
    a = client.post(f"/api/turn/{r['turn_id']}/analyze", headers=auth(s)).json()
    assert a["guidance"]
    with db.tx() as conn:
        idx = [row["idx"] for row in db.all_(conn, "select idx from turns where session_id=%s "
                                                   "order by idx", (s["session_id"],))]
    assert idx == list(range(len(idx)))                        # no gaps, no collisions


def test_deflection_goes_to_do_not_raise(client, twin, fake_llm):
    s = start(client, twin)
    fake_llm.on("observer", lambda ins, inp: {
        "facts": [], "topics": ["father"], "register": "reacting", "new_interests": [],
        "deflection": True, "deflected_topic": "father", "lexical_engagement": "low",
        "slot_updates": []})
    r = say(client, s, "I'd rather not talk about him.", "d-001")
    a = client.post(f"/api/turn/{r['turn_id']}/analyze", headers=auth(s)).json()
    assert a["policy"] == "drop_topic"
    cov = client.get(f"/api/twins/{twin['id']}/coverage", headers=CREATOR).json()
    assert {"topic": "father", "reason": "deflected"} in cov["do_not_raise"]


def test_slot_updates_fill_coverage(client, twin, fake_llm):
    s = start(client, twin)
    fake_llm.on("observer", lambda ins, inp: {
        "facts": [{"kind": "fact", "text": "Roasts jeera until it smells like toast"}],
        "topics": ["dal"], "register": "instructing", "new_interests": ["cooking"],
        "deflection": False, "deflected_topic": None, "lexical_engagement": "high",
        "slot_updates": [{"domain_id": "recipes", "item": "Dal", "slot": "_items", "status": "done"},
                         {"domain_id": "recipes", "item": "Dal", "slot": "steps", "status": "partial"},
                         {"domain_id": "nope", "item": "", "slot": "x", "status": "done"}]})
    r = say(client, s, "For dal, first roast jeera, then add ghee and the lentils.", "s-001")
    client.post(f"/api/turn/{r['turn_id']}/analyze", headers=auth(s))
    cov = client.get(f"/api/twins/{twin['id']}/coverage", headers=CREATOR).json()
    slots = {(x["domain_id"], x["item"], x["slot"]): x["status"] for x in cov["slots"]}
    assert slots[("recipes", "dal", "steps")] == "partial"
    assert ("nope", "", "x") not in slots
    assert cov["notes"][0]["text"].startswith("Roasts jeera")


def test_end_text_session_runs_pipeline(client, twin):
    s = start(client, twin)
    say(client, s, "One answer that never got analyzed by the browser.", "e-001")
    r = client.post(f"/api/sessions/{s['session_id']}/end", json={"reason": "user"}, headers=auth(s))
    assert r.json()["status"] == "ended"
    with db.tx() as conn:
        sess = db.one(conn, "select status from sessions where id=%s", (s["session_id"],))
        unanalyzed = db.one(conn, "select count(*) n from turns where session_id=%s and "
                                  "speaker='subject' and analyzed_at is null", (s["session_id"],))["n"]
        jobs = {r["kind"]: r["status"] for r in db.all_(conn, "select kind, status from jobs")}
    assert sess["status"] == "processed" and unanalyzed == 0
    assert jobs == {"analyze_backlog": "done", "coverage_update": "done"}
    debug = client.get(f"/api/twins/{twin['id']}/sessions/{s['session_id']}/debug",
                       headers=CREATOR).json()
    assert debug["talk_share"] is not None and debug["turns"][1]["decision"]


def test_text_session_resumes(client, twin):
    s1 = start(client, twin)
    s2 = start(client, twin)
    assert s2["session_id"] == s1["session_id"] and s2["resumed"]


def test_idle_sessions_are_swept(client, twin):
    s = start(client, twin)
    with db.tx() as conn:
        conn.execute("update sessions set last_activity_at=%s where id=%s",
                     (datetime.now(UTC) - timedelta(hours=13), s["session_id"]))
    r = client.get("/api/cron/daily", headers={"authorization": "Bearer cron-secret"})
    assert r.status_code == 200 and r.json()["ended_sessions"] == 1
    assert client.get("/api/cron/daily").status_code == 401
    with db.tx() as conn:
        row = db.one(conn, "select end_reason from sessions where id=%s", (s["session_id"],))
    assert row["end_reason"] == "idle_timeout"


def test_voice_session_segments_and_alignment(client, twin):
    s = start(client, twin, "voice")
    assert s["max_duration_s"] == 30 * 60
    # two overlapping segments uploaded through local storage
    for seq, (a, b) in enumerate([(0, 60000), (58000, 90000)]):
        sign = client.post("/api/uploads/sign", json={"seq": seq}, headers=auth(s)).json()
        put_path = sign["url"].split("http://testserver", 1)[1]
        assert client.put(put_path, content=b"fake-webm").status_code == 200
        done = client.post("/api/uploads/complete", headers=auth(s), json={
            "seq": seq, "path": sign["path"], "start_ms": a, "end_ms": b, "bytes": 9})
        assert done.status_code == 200
    r = say(client, s, "I learned it from my aunt", "v-001",
            interviewer={"text": "Who taught you?", "start_ms": 1000, "end_ms": 2500},
            features={"voiced_s": 3.2, "energy_mean_db": -30, "ttfv_s": 0.8})
    assert r["reply"] is None
    client.post(f"/api/turn/{r['turn_id']}/analyze", headers=auth(s))
    client.post(f"/api/sessions/{s['session_id']}/end", headers=auth(s),
                json={"expected_segments": 2})
    # no Deepgram key → pipeline skips transcription but still finishes
    with db.tx() as conn:
        assert db.one(conn, "select status from sessions where id=%s",
                      (s["session_id"],))["status"] == "processed"
        turns = db.all_(conn, "select id, speaker, start_ms, end_ms from turns where session_id=%s "
                              "order by idx", (s["session_id"],))
        assets = db.all_(conn, "select id, seq from audio_assets order by seq")
    subj = [t for t in turns if t["speaker"] == "subject"][0]
    assert subj["start_ms"] is None   # test posted no timings for the subject; give it some
    with db.tx() as conn:
        conn.execute("update turns set start_ms=3000, end_ms=6000 where id=%s", (subj["id"],))
    # simulate Deepgram callbacks (segment-relative times)
    bodies = [
        [("Who", 1.0), ("I", 3.1), ("learned", 3.4), ("umm", 3.8)],      # "Who" = echo
        [("it", 0.0 + 1.2), ("from", 1.6), ("aunt", 2.0)],              # +58 s offset
    ]
    for asset, words in zip(assets, bodies, strict=True):
        body = {"results": {"channels": [{"alternatives": [{"words": [
            {"word": w, "punctuated_word": w, "start": t, "end": t + 0.2} for w, t in words]}]}]}}
        r = client.post(f"/api/webhooks/deepgram?asset={asset['id']}&secret=dg-secret", json=body)
        assert r.status_code == 200
    assert client.post(f"/api/webhooks/deepgram?asset={assets[0]['id']}&secret=bad",
                       json={}).status_code == 401
    with db.tx() as conn:
        t = db.one(conn, "select text_corpus, words from turns where id=%s", (subj["id"],))
        sig = db.one(conn, "select count(*) n from signals where source='deepgram_timing'")["n"]
    assert t["text_corpus"].startswith("I learned umm") and "Who" not in t["text_corpus"]
    assert sig > 0


def test_voice_fails_closed_without_redis(client, twin):
    class Down(cache.MemoryBackend):
        def get(self, key):
            raise cache.CacheUnavailable("down")

        def incr_by(self, key, amount, ttl_s):
            raise cache.CacheUnavailable("down")

    cache.set_backend(Down())
    r = client.post("/api/sessions", json={"invite_token": twin["token"], "modality": "voice"})
    assert r.status_code == 429 and "paused" in r.json()["detail"]


def test_voice_connect_needs_key(client, twin):
    s = start(client, twin, "voice")
    r = client.post("/api/voice/connect", json={"sdp": "v=0"}, headers=auth(s))
    assert r.status_code == 503 and "OPENAI_API_KEY" in r.json()["detail"]


def test_delegation_recalls_notes(client, twin, fake_llm):
    s = start(client, twin, "voice")
    with db.tx() as conn:
        conn.execute("insert into notes (twin_id, kind, text) values (%s,'fact',%s)",
                     (twin["id"], "Aunt Kamla taught her to cook in Jaipur"))
    r = client.post("/api/delegation", headers=auth(s),
                    json={"delegation_id": "d1", "last_subject_text": "my aunt Kamla"}).json()
    assert "Kamla" in r["content"]


def test_import_whatsapp(client, twin):
    r = client.post(f"/api/twins/{twin['id']}/imports", headers=CREATOR,
                    json={"kind": "whatsapp", "filename": "chat.txt", "author_name": "Mom"}).json()
    path = r["upload"]["url"].split("http://testserver", 1)[1]
    text = "12/03/2024, 21:15 - Mom: Beta khana khaya?\n12/03/2024, 21:16 - Me: haan\n"
    assert client.put(path, content=text.encode()).status_code == 200
    client.post(f"/api/twins/{twin['id']}/imports/{r['import_id']}/complete", headers=CREATOR)
    with db.tx() as conn:
        rows = db.all_(conn, "select text, embedding is not null emb from corpus_items")
    assert rows == [{"text": "Beta khana khaya?", "emb": True}]


def test_ask_twin(client, twin, fake_llm):
    fake_llm.on("twin", lambda ins, inp: f"answer to {inp}")
    r = client.post(f"/api/twins/{twin['id']}/ask", headers=CREATOR, json={"question": "dal?"})
    assert r.json()["answer"] == "answer to dal?"


def test_voice_examples_reach_the_twin_as_turns(client, twin, fake_llm):
    s = start(client, twin)
    answers = [f"So, like, story number {i} is about the old house and, like, the mango tree we "
               f"had in the yard, which my father planted the year I was born." for i in range(4)]
    for i, a in enumerate(answers):
        say(client, s, a, f"v-00{i}")
    fake_llm.on("speech_habits", lambda _i, _x: {
        "items": [{"phrase": "like", "use": "filler"}],
        "examples": [{"index": 0, "why": "filler and family"}, {"index": 2, "why": "rhythm"}]})
    client.post(f"/api/sessions/{s['session_id']}/end", json={"reason": "user"}, headers=auth(s))
    with db.tx() as conn:
        ex = db.all_(conn, "select question, answer from voice_examples where twin_id=%s",
                     (twin["id"],))
    assert [e["answer"] for e in ex][:2] == [answers[0], answers[2]] and len(ex) == 4
    assert all(e["question"] for e in ex)
    seen = {}
    fake_llm.on("twin", lambda ins, inp: seen.setdefault("input", inp) and "ok")
    client.post(f"/api/twins/{twin['id']}/ask", headers=CREATOR, json={"question": "dal?"})
    assert answers[0] in seen["input"] and seen["input"].endswith("dal?")


def test_twin_answers_at_their_usual_length(client, twin, fake_llm):
    s = start(client, twin)
    for i in range(6):
        say(client, s, " ".join(["word"] * (10 + i)), f"len-{i:03}")
    seen = {}
    fake_llm.on("twin", lambda ins, inp: seen.setdefault("ins", ins) and "ok")
    client.post(f"/api/twins/{twin['id']}/ask", headers=CREATOR, json={"question": "dal?"})
    assert "usually about 13 words, rarely over 15" in seen["ins"]


def test_persona_reacts_to_interviewer_turns(client, fake_llm):
    fake_llm.on("sim_persona", lambda ins, inp: {"effect": 2, "reason": "picked up the floods"})
    body = {"persona": "venkat", "energy": 2, "elapsed_s": 60,
            "exchange": [{"who": "them", "text": "Floods, 2015."},
                         {"who": "interviewer", "text": "You kept the branch open?"}]}
    r = client.post("/api/dev/persona/react", json=body).json()
    assert r["energy"] == 4 and r["mood"] == "smiling" and r["cue"]   # bored → smiling: new cue
    body.update(energy=4)
    assert client.post("/api/dev/persona/react", json=body).json()["cue"] is None  # still smiling
    fake_llm.on("sim_persona", lambda ins, inp: {"effect": -2, "reason": "generic"})
    body.update(energy=4.5, elapsed_s=400)                                  # past stamina
    r = client.post("/api/dev/persona/react", json=body).json()
    assert r["mood"] == "exhausted" and "tired" in r["cue"]


def test_static_files_are_revalidated(client):
    r = client.get("/voice.js")
    assert r.status_code == 200 and r.headers["cache-control"] == "no-cache"


def test_vercel_entry_restores_the_rewritten_path(database_url):
    from fastapi.testclient import TestClient

    from api.index import app

    c = TestClient(app)
    r = c.get("/api/index", params={"__path": "/api/public-config", "x": "1"})
    assert r.status_code == 200 and "supabase_url" in r.json()


def test_open_access_shares_everything_without_sign_in(client, monkeypatch):
    from app.settings import get_settings

    monkeypatch.setenv("OPEN_ACCESS", "1")
    monkeypatch.setenv("DEV_AUTH", "")
    get_settings.cache_clear()
    assert client.get("/api/public-config").json()["open_access"] is True
    r = client.post("/api/twins", json={"brief": "A twin of my grandfather and his stories."})
    assert r.status_code == 200                                    # no headers, no sign-in
    assert [t["id"] for t in client.get("/api/twins").json()] == [r.json()["twin_id"]]
    monkeypatch.undo()
    get_settings.cache_clear()


def test_simulator_needs_its_key_when_one_is_set(client, monkeypatch):
    from app.settings import get_settings

    monkeypatch.setenv("SIM_KEY", "s3cret-key")
    get_settings.cache_clear()
    assert client.get("/api/dev/personas").status_code == 404      # looks like nothing is there
    assert client.get("/api/dev/personas", headers={"x-sim-key": "wrong"}).status_code == 404
    assert client.get("/api/dev/personas", headers={"x-sim-key": "s3cret-key"}).status_code == 200
    assert client.get("/api/dev/personas?key=s3cret-key").status_code == 200  # audio/link form
    assert client.get("/dev/sim").status_code == 404
    monkeypatch.undo()
    get_settings.cache_clear()


def test_simulated_twins_join_the_shared_space_with_a_label(client, monkeypatch):
    from app.settings import get_settings

    monkeypatch.setenv("OPEN_ACCESS", "1")
    monkeypatch.setenv("DEV_AUTH", "")
    get_settings.cache_clear()
    real = client.post("/api/twins", json={"brief": "A twin of my grandfather and his stories."})
    sim = client.post("/api/dev/sim/setup", json={"persona": "kavya"}).json()
    listed = {t["id"]: t["simulated"] for t in client.get("/api/twins").json()}
    assert listed == {real.json()["twin_id"]: False, sim["twin_id"]: True}
    monkeypatch.undo()
    get_settings.cache_clear()



def test_whole_call_recording_is_kept_apart_and_playable(client, twin):
    from tests.conftest import CREATOR

    s = start(client, twin, "voice")
    rec = f"/api/twins/{twin['id']}/sessions/{s['session_id']}/recording"
    assert client.get(rec, headers=CREATOR).json()["parts"] == []
    for kind, seq in [("segment", 0), ("call", 0), ("call", 1)]:
        sign = client.post("/api/uploads/sign", json={"seq": seq, "kind": kind},
                           headers=auth(s)).json()
        assert ("/call-" in sign["path"]) == (kind == "call")   # never overwrites a mic segment
        client.put(sign["url"].split("http://testserver", 1)[1], content=b"fake-webm")
        assert client.post("/api/uploads/complete", headers=auth(s), json={
            "seq": seq, "kind": kind, "path": sign["path"], "start_ms": seq * 600_000,
            "end_ms": (seq + 1) * 600_000}).status_code == 200
    got = client.get(rec, headers=CREATOR).json()
    assert got["source"] == "call" and len(got["parts"]) == 2
    assert client.get(got["parts"][1]["url"].split("http://testserver", 1)[1]).content == b"fake-webm"
    with db.tx() as conn:   # transcription still only sees the mic segment
        assert db.one(conn, "select count(*) n from audio_assets where kind='segment'")["n"] == 1
