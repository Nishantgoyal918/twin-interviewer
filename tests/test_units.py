"""Pure-function tests: signals, alignment, style, imports, tokens, policy, coverage."""

from __future__ import annotations

import time

import pytest

from app.fakes import fake_spec
from app.models import ObserverOutput, SessionBudget, SessionState, Tag, TwinSpec
from app.offline.align import align, merge_segments
from app.offline.imports import parse_whatsapp
from app.offline.style import is_stable, style_stats
from app.planner import coverage, policy
from app.planner.coverage import TwinState
from app.planner.planner import plan
from app.security.tokens import BadToken, sign_session, verify_session
from app.signals import baseline
from app.signals.tags import make_tags
from app.signals.text_signals import text_features
from app.signals.timing_features import timing_features


def obs(**kw) -> ObserverOutput:
    base = dict(facts=[], topics=[], register="storytelling", new_interests=[], deflection=False,
                deflected_topic=None, lexical_engagement="medium", slot_updates=[])
    base.update(kw)
    return ObserverOutput.model_validate(base)


def spec() -> TwinSpec:
    return TwinSpec.model_validate(fake_spec("a twin of my mother"))


def test_welford_matches_mean_and_z():
    stats = {}
    for x in [10, 12, 14, 16, 18]:
        stats = baseline.update(stats, {"reply_words": x})
    s = stats["reply_words"]
    assert s["mean"] == pytest.approx(14)
    assert baseline.std(s) == pytest.approx(3.1623, rel=1e-3)
    assert baseline.zscore(s, 20.3246) == pytest.approx(2.0, rel=1e-3)
    assert baseline.zscore({"n": 2, "mean": 1, "m2": 1}, 5) is None   # too few samples


def test_cold_start_tags_are_capped_below_action_threshold():
    session = {}
    for x in [10, 11, 9, 10, 12]:
        session = baseline.update(session, {"reply_words": x})
    tags = make_tags({"reply_words": 40}, twin_stats={}, session_stats=session)
    assert tags and all(t.confidence <= 0.4 for t in tags)


def test_long_pause_tag():
    tags = make_tags({"ttfv_s": 3.1}, {}, {})
    assert tags[0].tag == "long_pause_before_answer" and tags[0].confidence == 0.9


def test_text_features():
    f = text_features("haha yes! and also the dal? 😂",
                      [{"text": "haha yes!", "at_ms": 2000}, {"text": "and also the dal? 😂", "at_ms": 5000}])
    assert f["burst_count"] == 2 and f["questions_back"] == 1
    assert f["laughter"] >= 1 and f["within_burst_latency_s"] == 3.0


def test_timing_features_code_switch_and_fillers():
    words = [["um", 0, 200], ["मैं", 300, 500], ["went", 900, 1100], ["there", 1150, 1400]]
    f = timing_features(words)
    assert f["filler_rate"] == 0.25
    assert f["code_switch_rate"] == pytest.approx(2 / 3)
    assert f["pause_count"] == 1


def test_merge_segments_dedupes_overlap():
    a = {"start_ms": 0, "end_ms": 60000, "words": [["one", 1000, 1200], ["edge", 58800, 59200]]}
    b = {"start_ms": 58000, "end_ms": 118000, "words": [["edge", 58800, 59200], ["two", 61000, 61200]]}
    merged = merge_segments([b, a])
    assert [w[0] for w in merged] == ["one", "edge", "two"]


def test_align_drops_echo_and_assigns_nearest():
    turns = [
        {"id": "i1", "speaker": "interviewer", "start_ms": 0, "end_ms": 2000},
        {"id": "s1", "speaker": "subject", "start_ms": 2500, "end_ms": 6000},
        {"id": "s2", "speaker": "subject", "start_ms": 9000, "end_ms": 12000},
    ]
    words = [["echo", 500, 800], ["hello", 2600, 2900], ["late", 7000, 7200], ["bye", 9500, 9800]]
    out = align(words, turns)
    assert [w[0] for w in out["s1"]] == ["hello", "late"]
    assert [w[0] for w in out["s2"]] == ["bye"]


def test_style_stability():
    h = [{"stats": {"a": 10.0}}, {"stats": {"a": 10.5}}, {"stats": {"a": 9.8}}]
    assert is_stable(h, 3, 0.15)
    assert not is_stable(h + [{"stats": {"a": 20}}], 3, 0.15)
    assert style_stats(["Um so I went. Then I came back!"])["words_per_sentence"] == 4


def test_whatsapp_parser_keeps_only_author():
    text = ("12/03/2024, 21:15 - Mom: Beta khana khaya?\n"
            "12/03/2024, 21:16 - Me: haan\n"
            "12/03/2024, 21:17 - Mom: Accha. Dal mein\nthoda ghee daalna\n"
            "12/03/2024, 21:18 - Mom: <Media omitted>\n")
    assert parse_whatsapp(text, "mom") == ["Beta khana khaya?", "Accha. Dal mein\nthoda ghee daalna"]


def test_session_token_roundtrip_and_tamper():
    tok = sign_session("s1", "t1", 60)
    assert verify_session(tok).session_id == "s1"
    with pytest.raises(BadToken):
        verify_session(tok[:-2] + "xx")
    expired = sign_session("s1", "t1", -1)
    time.sleep(0.01)
    with pytest.raises(BadToken):
        verify_session(expired)


def test_turn_state_and_trend():
    assert policy.turn_state(obs(deflection=True), [], 5, []) == "avoidant"
    assert policy.turn_state(obs(lexical_engagement="high"), [], 80, [60, 70]) == "engaged"
    assert policy.turn_state(obs(lexical_engagement="low"), [], 3, [60, 70]) == "flat"
    assert policy.turn_state(obs(lexical_engagement="unclear"), [], 3, []) == "uncertain"
    up = Tag(tag="reply_words", value="+2", z=2, confidence=0.8)
    assert policy.turn_state(obs(lexical_engagement="medium"), [up, up], 40, []) == "engaged"
    # one flat turn is not a trend
    assert policy.trend_state(["engaged", "flat"], 60, 20) == "uncertain"
    assert policy.trend_state(["flat", "uncertain", "flat"], 60, 20) == "flat"
    assert policy.trend_state(["engaged"], 20 * 60, 20) == "tired"
    # one clearly engaged answer ends a flat stretch at once (no lag after warming up)
    assert policy.trend_state(["flat", "flat", "engaged"], 60, 20) == "engaged"
    assert policy.trend_state(["engaged", "flat", "flat"], 60, 20) == "flat"
    # late in the chat, only a solid flat run ends it early; a dip of two doesn't
    late = 0.75 * 20 * 60
    assert policy.trend_state(["engaged", "flat", "flat"], late, 20) == "flat"
    assert policy.trend_state(["flat", "flat", "flat"], late, 20) == "tired"


def test_coverage_goals_skip_do_not_raise():
    s = spec()
    ts = TwinState(do_not_raise={"life story"})
    goals = coverage.content_goals(s, ts)
    assert goals and all(g.domain_id != "life_story" for g in goals)
    assert goals[0].slot == "_items"     # open-list domain with no items yet


def _state(s: TwinSpec) -> SessionState:
    return SessionState(session_id="s", twin_id="t", modality="text",
                        session_budget=SessionBudget(target_minutes=20, max_duration_s=1800))


def test_planner_drops_deflected_topic_and_moves_on():
    s = spec()
    state, ts = _state(s), TwinState()
    d = plan(s, state, ts, obs(deflection=True, deflected_topic="father", topics=["father"]), [], 4, 1)
    assert d.policy == "drop_topic" and "father" in ts.do_not_raise
    assert "never bring it back" in d.guidance


def test_planner_lets_engaged_run_and_wraps_when_tired():
    s = spec()
    state, ts = _state(s), TwinState()
    for i in range(3):
        d = plan(s, state, ts, obs(lexical_engagement="high", topics=["diwali"]), [], 120, i)
    assert d.policy == "let_them_run" and "diwali" in state.bridge_sources
    state.session_budget.elapsed_s = 21 * 60
    d = plan(s, state, ts, obs(), [], 30, 4)
    assert d.policy == "wrap_up" and d.wrap_up


def test_planner_flat_changes_kind_of_question():
    s = spec()
    state, ts = _state(s), TwinState()
    for i in range(3):
        d = plan(s, state, ts, obs(lexical_engagement="low"), [], 3, i)
    assert d.policy == "change_kind" and d.goal.kind in ("choice", "register")


def test_echo_dropped_but_barge_in_kept():
    q = {"id": "q", "speaker": "interviewer", "start_ms": 0, "end_ms": 4000,
         "text": "So who taught you to cook and what was the first dish?"}
    a = {"id": "a", "speaker": "subject", "start_ms": 2500, "end_ms": 7000}
    # echo of the question's second half overlaps the subject's barge-in
    words = [["and", 2600, 2700], ["what", 2750, 2900], ["was", 2950, 3050], ["Aunt", 2800, 3000],
             ["Kamla!", 3050, 3400], ["the", 3300, 3400], ["first", 3450, 3600],
             ["dish?", 3650, 3900], ["The", 5000, 5100], ["first", 5150, 5300], ["thing", 5350, 5600]]
    kept = [w[0] for w in align(words, [q, a])["a"]]
    assert kept == ["Aunt", "Kamla!", "The", "first", "thing"]


def test_deepgram_language_follows_subject():
    from app.offline.deepgram import params

    en = dict(params(["en"]))
    assert en["language"] == "en" and en["sentiment"] == "true"
    mixed = params(["hi", "en"])
    assert ("language", "multi") in mixed and ("language_hint", "hi") in mixed
    assert "sentiment" not in dict(mixed)


def test_sentiment_and_unreliable_fillers():
    words = [["Um,", 0, 200, -0.4], ["she", 300, 500, -0.6], ["was", 550, 700, -0.5]]
    f = timing_features(words)
    assert f["sentiment"] == pytest.approx(-0.5) and f["filler_rate"] == pytest.approx(1 / 3)
    assert "filler_rate" not in timing_features(words, fillers_reliable=False)


def test_deflected_topic_leaves_no_trace():
    from app.observer import clean

    o = obs(deflection=True, deflected_topic="Father", new_interests=["father's shop", "cooking"],
            facts=[{"kind": "fact", "text": "Mentioned her father briefly"},
                   {"kind": "fact", "text": "Cooks dal on Sundays"}])
    out = clean(o, spec())
    assert [f.text for f in out.facts] == ["Cooks dal on Sundays"]
    assert out.new_interests == ["cooking"]


def test_interviewer_introduces_itself_only_once():
    from app.conversation import build_instructions

    s = spec()
    kw = dict(notes="", do_not_raise=[], guidance="", first_session=True, modality="text")
    assert "you're an AI interviewer" in build_instructions(s, **kw, opening=True)
    later = build_instructions(s, **kw, opening=False)
    assert "you're an AI interviewer" not in later and "Never introduce yourself" in later


def test_speech_habits_keep_only_real_repeats(fake_llm):
    from app.offline.style import habit_line, phrase_hits, speech_habits

    assert phrase_hits("Correct? Correct, correct. Incorrect.", "correct") == 3
    assert phrase_hits("I like, like, liked it", "like") == 2
    texts = ["So, like, I was like totally not ready, you know, for the move at all honestly.",
             "It was literally, like, the best cake I have ever baked in my whole entire life.",
             "Mango is, like, the goodest boy and my parents will, like, spoil him rotten there.",
             "Toronto has, like, amazing bakeries and I'm literally going to try every single one.",
             "Work has been busy this quarter because we are shipping a new payments feature soon.",
             "My parents live in Mysuru and they visit every couple of months to see us."]
    fake_llm.on("speech_habits", lambda _i, _x: {"items": [
        {"phrase": "Like", "use": "filler in most sentences"},
        {"phrase": "literally", "use": "for emphasis"},          # 2 uses: below min_count
        {"phrase": "you know what i mean", "use": "invented"},  # never said
    ]})
    habits = speech_habits(texts)
    assert [h["phrase"] for h in habits] == ["like"]
    assert habits[0]["count"] == 6 and habits[0]["per_100_words"] > 6
    assert '"like"' in habit_line(habits[0])
    assert speech_habits(["too short"]) == []


def test_inline_jobs_retry_like_qstash(monkeypatch):
    from app import queue

    calls = []

    def flaky(job):
        calls.append(1)
        if len(calls) < 3:
            raise RuntimeError("lock busy")

    monkeypatch.setitem(queue.HANDLERS, "flaky_test", flaky)
    monkeypatch.setattr(queue.time, "sleep", lambda _s: None)
    queue.enqueue("flaky_test", "00000000-0000-0000-0000-00000000f1a2")
    assert len(calls) == 3


def test_voice_examples_skip_flat_and_short_answers(fake_llm):
    from app.offline.style import voice_profile

    long = ("So, like, we used to walk to the lake every single evening after dinner, like always. "
            "My brother would race ahead and, like, hide behind the banyan tree to scare us.")
    pairs = [{"turn_id": f"t{i}", "question": f"Q{i}?", "answer": long, "low_energy": False}
             for i in range(4)]
    pairs[1]["low_energy"] = True                      # a tired moment, not their voice
    pairs[2]["answer"] = "Yes, like, sure."            # too short to show much
    fake_llm.on("speech_habits", lambda _i, _x: {
        "items": [{"phrase": "like", "use": "filler"}],
        "examples": [{"index": i, "why": "w"} for i in (1, 2, 3, 3, 9)]})
    habits, examples = voice_profile(pairs)
    assert [h["phrase"] for h in habits] == ["like"]
    assert [e["turn_id"] for e in examples] == ["t3", "t0"]  # model pick, then a top-up
    assert examples[0]["question"] == "Q3?" and examples[0]["why"] == "w"
    # nothing usable picked → fall back to the full answers densest in their habits
    fake_llm.on("speech_habits", lambda _i, _x: {
        "items": [{"phrase": "like", "use": "filler"}], "examples": []})
    assert [e["turn_id"] for e in voice_profile(pairs)[1]] == ["t0", "t3"]


def test_voice_examples_skip_split_turns_and_passing_moods(fake_llm):
    from app.offline.style import voice_profile

    base = ("Like, the bakery on Queen Street does, like, the flakiest croissants I have ever had. "
            "My friends say I will, like, live there all winter and honestly they are right.")
    answers = [base, "croissants. " + base, "Honestly, like, I'm kinda wiped today but " + base,
               "So " + base]
    pairs = [{"turn_id": f"t{i}", "question": "Q?", "answer": a} for i, a in enumerate(answers)]
    fake_llm.on("speech_habits", lambda _i, _x: {
        "items": [], "examples": [{"index": i, "why": ""} for i in range(4)]})
    assert [e["turn_id"] for e in voice_profile(pairs)[1]] == ["t0", "t3"]


def test_reactive_persona_energy_and_mood():
    from app.routers.dev import mood_for, react_step

    assert [mood_for(e, False) for e in (0, 3, 4, 6, 7, 10)] == \
        ["bored", "bored", "smiling", "smiling", "excited", "excited"]
    assert mood_for(2, True) == "exhausted" and mood_for(8, True) == "excited"
    p = {"reactive": {"start": 2, "stamina_s": 300}}
    assert react_step(p, 2, 100, +2) == {"energy": 4, "fatigue": 0.0, "mood": "smiling"}
    # past stamina, a neutral turn costs energy; nothing goes below 0 or above 10
    assert react_step(p, 3.5, 400, 0) == {"energy": 3.0, "fatigue": 0.5, "mood": "exhausted"}
    assert react_step(p, 0.2, 400, -2)["energy"] == 0
    assert react_step(p, 9.5, 100, +2)["energy"] == 10


def test_engagement_summary():
    from scripts.sim_score import engagement

    energy = [{"ms": 1000, "energy": 4, "effect": 2, "fatigue": 0, "reason": "detail", "interviewer": "a"},
              {"ms": 2000, "energy": 3, "effect": -1, "fatigue": 0, "reason": "generic", "interviewer": "b"},
              {"ms": 3000, "energy": 2.5, "effect": 0, "fatigue": 0.5, "reason": "ok", "interviewer": "c"}]
    en = engagement(energy, 2)
    assert en["net"] == 1 and en["fatigue"] == 0.5 and en["end"] == 2.5
    assert en["engaged_share"] == 0.25 and [e["reason"] for e in en["lifts"]] == ["detail"]
    assert [e["reason"] for e in en["drops"]] == ["generic"]


def test_supabase_pooler_url_is_cleaned():
    from app.settings import _clean_db_url

    assert _clean_db_url("postgres://u:p@h:6543/db?sslmode=require&supa=base-pooler.x") == \
        "postgres://u:p@h:6543/db?sslmode=require"
    assert _clean_db_url("postgres://u:p@h/db") == "postgres://u:p@h/db"
    assert _clean_db_url("") == ""


def test_planner_bridges_from_an_engaged_thread_to_an_untouched_topic():
    s = spec()
    state, ts = _state(s), TwinState()
    policies = [plan(s, state, ts, obs(lexical_engagement="high", topics=["diwali"]), [], 120, i)
                for i in range(4)]
    assert [d.policy for d in policies[1:3]] == ["let_them_run", "let_them_run"]
    d = policies[3]   # a happy person who is never steered gives a twin that knows one topic
    assert d.policy == "bridge" and d.goal.domain_id == "recipes" and d.firm
    assert "bridge from what they just said" in d.guidance


def test_planner_gives_up_on_a_goal_that_never_comes_up():
    s = spec()
    state, ts = _state(s), TwinState()
    goals = []
    for i in range(6):   # 30 s apart; patience is in time (answers come in fragments)
        state.session_budget.elapsed_s = 30 * i
        goals.append(plan(s, state, ts, obs(), [], 40, i).goal.id)
    assert goals[0] == goals[2] == "life_story::childhood"
    assert goals[3] != goals[0] and goals[0] in state.skipped_goals


def test_planner_presses_for_untouched_topics_late_in_the_call():
    s = spec()
    state, ts = _state(s), TwinState()
    plan(s, state, ts, obs(), [], 40, 0)
    for i in range(1, 4):                           # past half of 20 minutes
        state.session_budget.elapsed_s = 12 * 60 + 30 * i
        d = plan(s, state, ts, obs(), [], 40, i)
    assert d.policy == "continue" and "haven't come up yet" in d.guidance and d.firm


def test_cut_off_fragment_is_not_read_as_flat():
    low = Tag(tag="energy_mean_db", value="-2", z=-2, confidence=0.8)
    assert policy.turn_state(obs(lexical_engagement="unclear"), [low], 2, [40, 50]) == "uncertain"
    assert policy.turn_state(obs(lexical_engagement="low"), [low], 2, [40, 50]) == "flat"
