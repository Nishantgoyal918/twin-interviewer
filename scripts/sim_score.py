"""Scorecard for a simulated interview (devtools/sim). Compares what the platform did and
learned against the persona's hidden card. Writes .data/sim/<session>.md and prints it.

    uv run python scripts/sim_score.py <session_id> [<session_id> ...]
    uv run python scripts/sim_score.py --all          # every saved run
"""

from __future__ import annotations

import json
import os
import sys
from collections import Counter
from pathlib import Path

import yaml
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
RUNS = ROOT / ".data" / "sim"

# What a good interviewer's planner should conclude in each mood.
EXPECTED = {"excited": {"engaged"}, "smiling": {"engaged", "uncertain"},
            "bored": {"flat", "uncertain"}, "exhausted": {"flat", "tired", "uncertain"}}


class Judged(BaseModel):
    index: int
    hit: bool
    evidence: str


class Judgements(BaseModel):
    items: list[Judged]


class Raised(BaseModel):
    raised_before_refusal: bool
    raised_after_refusal: bool
    refusal_happened: bool
    evidence: str


class TurnQ(BaseModel):
    index: int
    echoes: bool  # restates or quotes back what they just said
    questions: int
    bare_nudge: bool  # "tell me more"-style prompt after a short or low-energy answer
    reacts: bool  # a genuine reaction or small aside of its own
    note: str


class TurnQs(BaseModel):
    items: list[TurnQ]


LONG_TURN = 60  # words
# Open questions the twin answers only to measure its style (habit rate, length); three short
# probe answers are too few words for a stable per-100-words rate.
STYLE_QS = ["What do you do on a normal weekend?", "Tell me about your friends.",
            "What's something that annoys you?", "What food do you love?",
            "How was your week?", "What would you tell someone younger than you?",
            "What are you looking forward to?", "Describe your family."]


def is_backchannel(text: str | None) -> bool:
    """'Mm.', 'Okay.': listening noises, not turns to grade."""
    return len((text or "").split()) <= 2 and "?" not in (text or "")


def judge_turns(pairs: list[tuple[str, str, str]]) -> list[TurnQ]:
    """Per interviewer turn: is it a good conversational turn? Length alone is not a fault."""
    from app.llm import llm

    numbered = "\n\n".join(f"{i}. SUBJECT ({mood}): {ans}\n   INTERVIEWER: {q}"
                           for i, (mood, ans, q) in enumerate(pairs))
    out = llm().json("sim_judge", (
        "You grade an interviewer's turns, each shown after the subject's previous answer and "
        "mood. For every index report: echoes = it repeats or paraphrases what the subject "
        "just said back to them as a statement ('So you grew up in Dadar and...'); asking about "
        "something they mentioned, or reusing one of their words in a new question, is NOT an "
        "echo; questions = how many distinct "
        "questions it asks (an either/or choice like 'X or Y?' is one); "
        "bare_nudge = a generic prompt like 'tell me more' / 'go on' / 'what "
        "else' right after a short or low-energy answer, giving the subject nothing to react to; "
        "reacts = it shows a genuine reaction, opinion or small aside of its own; note = a few "
        "words on what's good or wrong."), numbered, TurnQs)
    by = {j.index: j for j in out.items}
    return [by.get(i, TurnQ(index=i, echoes=False, questions=1, bare_nudge=False, reacts=False,
                            note="not graded")) for i in range(len(pairs))]


def turn_flags(q: TurnQ, words: int) -> list[str]:
    return [f for f, on in (("echo", q.echoes), ("multi-question", q.questions > 1),
                            (f"long (>{LONG_TURN}w)", words > LONG_TURN),
                            ("bare nudge", q.bare_nudge)) if on]


def judge_list(items: list[str], context: str, question: str) -> list[Judged]:
    from app.llm import llm

    numbered = "\n".join(f"{i}. {t}" for i, t in enumerate(items))
    out = llm().json("sim_judge", f"You are a strict grader. {question} Answer for every item "
                     "by index; hit=true if the context clearly supports the main point, even if a "
                     "minor detail (a year, a small extra) is missing; misspelled names count. "
                     "Give a short "
                     "quote as evidence or ''.", f"ITEMS:\n{numbered}\n\nCONTEXT:\n{context}",
                     Judgements)
    by = {j.index: j for j in out.items}
    return [by.get(i, Judged(index=i, hit=False, evidence="")) for i in range(len(items))]


def phase_at(phases: list[dict], ms: int | None) -> str:
    cur = phases[0]["mood"] if phases else "?"
    for p in phases:
        if ms is not None and ms >= p["at_ms"]:
            cur = p["mood"]
    return cur


def engagement(energy: list[dict], start: float) -> dict:
    """Reactive runs: how much engagement the interviewer earned. `energy` has one entry per
    judged interviewer turn ({ms, energy, effect, fatigue, reason, interviewer})."""
    levels = [start, *(e["energy"] for e in energy)]
    ranked = sorted(energy, key=lambda e: e["effect"])
    return {"mean": sum(levels) / len(levels), "start": start, "end": levels[-1],
            "engaged_share": sum(x >= 4 for x in levels) / len(levels),
            "net": sum(e["effect"] for e in energy),
            "fatigue": sum(e.get("fatigue") or 0 for e in energy),
            "lifts": [e for e in reversed(ranked) if e["effect"] > 0][:3],
            "drops": [e for e in ranked if e["effect"] < 0][:3],
            "effects": dict(Counter(e["effect"] for e in energy))}


def mean(xs):
    xs = [x for x in xs if isinstance(x, (int, float))]
    return sum(xs) / len(xs) if xs else None


def f2(x, nd=2):
    return "–" if x is None else f"{x:.{nd}f}"


def score(sid: str) -> str:
    from app import db, twin
    from app.llm import llm

    run = json.loads((RUNS / f"{sid}.json").read_text(encoding="utf-8"))
    card = yaml.safe_load((ROOT / "personas" / f"{run['persona']}.yaml").read_text(encoding="utf-8"))
    tid = run["twin_id"]
    with db.tx() as conn:
        s = db.one(conn, "select * from sessions where id=%s", (sid,))
        turns = db.all_(conn, """select t.id, t.idx, t.speaker, t.start_ms, t.end_ms, t.text_live,
                 t.text_corpus, t.features, p.decision from turns t
                 left join planner_snapshots p on p.turn_id = t.id
                 where t.session_id=%s order by t.idx""", (sid,))
        sigs = db.all_(conn, """select turn_id, kind, value from signals where session_id=%s
                                and source='deepgram_timing'""", (sid,))
        notes = [r["text"] for r in db.all_(conn, "select text from notes where twin_id=%s", (tid,))]
        dnr = db.all_(conn, "select topic, reason from do_not_raise where twin_id=%s", (tid,))
        spec = db.one(conn, """select spec from twin_specs where twin_id=%s
                               order by version desc limit 1""", (tid,))["spec"]
        slot_rows = db.all_(conn, """select domain_id, status, count(*) n from coverage_slots
                   where twin_id=%s and status <> 'empty' group by 1, 2""", (tid,))
    timing: dict = {}
    for g in sigs:
        timing.setdefault(str(g["turn_id"]), {})[g["kind"]] = (g["value"] or {}).get("raw")

    phases = run["phases"]
    subj = [t for t in turns if t["speaker"] == "subject"]
    inter = [t for t in turns if t["speaker"] == "interviewer"]
    w = lambda t: len((t["text_live"] or "").split())  # noqa: E731
    iw, sw = sum(map(w, inter)), sum(map(w, subj))
    questions = sum((t["text_live"] or "").count("?") for t in inter)

    # mood tracking
    rows, hits = [], 0
    by_phase: dict = {}
    for t in subj:
        mood = phase_at(phases, t["start_ms"])
        d = t["decision"] or {}
        eng, pol = d.get("engagement"), d.get("policy")
        ok = eng in EXPECTED.get(mood, set())
        hits += ok
        b = (t["features"] or {}).get("browser", {})
        tm = timing.get(str(t["id"]), {})
        by_phase.setdefault(mood, []).append({"eng": eng, "pol": pol, "words": w(t), "b": b, "tm": tm})
        rows.append(f"| {t['idx']} | {(t['start_ms'] or 0) / 1000:.0f}s | {mood} | {eng or '–'} | "
                    f"{pol or '–'} | {'✓' if ok else '✗'} | {(t['text_live'] or '')[:70]} |")
    wrap = next((t for t in subj if (t["decision"] or {}).get("wrap_up")), None)

    # turn quality: each interviewer turn after an answer (the opening has none)
    graded, prev = [], None
    for t in turns:
        if t["speaker"] == "subject":
            prev = t
        elif prev is not None and not is_backchannel(t["text_live"]):
            graded.append((t, prev))
    tq = judge_turns([(phase_at(phases, a["start_ms"]), a["text_live"] or "", t["text_live"] or "")
                      for t, a in graded])
    flags = [turn_flags(q, w(t)) for q, (t, _a) in zip(tq, graded, strict=True)]
    good = sum(not f for f in flags)
    flag_count = Counter(x.split(" ")[0] for f in flags for x in f)
    backchannels = sum(1 for t in inter if is_backchannel(t["text_live"]))
    tq_rows = [f"| {t['idx']} | {w(t)} | {', '.join(f) or '✓'} | {'yes' if q.reacts else '–'} | "
               f"{q.note} | {(t['text_live'] or '')[:80]} |"
               for q, f, (t, _a) in zip(tq, flags, graded, strict=True)]

    # knowledge capture, sensitivity, twin
    # GPT-Live's live text: Deepgram's offline text mangles names ("Lumding" -> "Longding")
    corpus = "\n".join(t["text_live"] or "" for t in subj)
    facts = judge_list(card["truth"], "NOTES:\n" + "\n".join(notes),
                       "Which of these facts about the person are captured in the interviewer's notes?")
    heard = judge_list(card["truth"], "TRANSCRIPT OF WHAT THEY SAID:\n" + corpus,
                       "Which of these facts did the person actually mention?")
    convo = "\n".join(f"{'INTERVIEWER' if t['speaker'] == 'interviewer' else 'SUBJECT'}: "
                      f"{t['text_live']}" for t in turns)
    raised = llm().json("sim_judge", "Grade the interviewer. The sensitive topic must not be "
                        "raised by the interviewer at all (the creator asked to avoid it), and "
                        "never again after the subject refuses it.",
                        f"SENSITIVE TOPIC: {card['sensitive']}\n\n{convo}", Raised)
    probes = []
    for pr in card["probes"]:
        ans = twin.answer(tid, pr["q"])
        probes.append((pr["q"], pr["a"], ans))
    pj = judge_list([f"Q: {q}\nEXPECTED: {a}\nTWIN SAID: {ans}" for q, a, ans in probes], "",
                    "Does the twin's answer match the expected answer in substance?")

    # speech habits: did they use it, did the platform capture it, does the twin use it,
    # and did the interviewer copy it (it must not)
    from app.offline.style import phrase_hits

    with db.tx() as conn:
        captured = [r["text"] for r in db.all_(conn, "select text from notes where twin_id=%s "
                                                     "and kind='speech_habit'", (tid,))]
    style_answers = [twin.answer(tid, q) for q in STYLE_QS]
    twin_text = " ".join([*(ans for _q, _a, ans in probes), *style_answers])
    real_len = sorted(len((t["text_live"] or "").split()) for t in subj)
    twin_len = sorted(len(a.split()) for a in style_answers)

    def rate(text, phrase):
        n = len(text.split())
        return 100 * phrase_hits(text, phrase) / n if n else 0.0

    habit_rows = []
    for h in card.get("habits", []):
        ph = h["phrase"]
        got = any(phrase_hits(c, ph) for c in captured)
        uses = [t["text_live"] for t in inter if phrase_hits(t["text_live"] or "", ph)]
        copied = sum(j.hit for j in judge_list(
            uses, f"The subject's habit: they say '{ph}' as a verbal tic.",
            f"In which of these interviewer lines is '{ph}' an imitation of the subject's habit "
            "(used the way they use it), rather than ordinary grammar like 'sounds like' or "
            "'I'd like'?")) if uses else 0
        habit_rows.append((ph, rate(corpus, ph), got, rate(twin_text, ph), len(uses), copied))

    # transcription agreement (GPT-Live live text vs Deepgram offline text)
    def overlap(a, b):
        A, B = set((a or "").lower().split()), set((b or "").lower().split())
        return len(A & B) / max(len(A | B), 1)
    tx = mean([overlap(t["text_live"], t["text_corpus"]) for t in subj if t["text_corpus"]])

    wrap_txt = (f"at {(wrap['start_ms'] or 0) / 1000:.0f}s, in {phase_at(phases, wrap['start_ms'])} "
                "phase" if wrap else "never")
    n_said = sum(j.hit for j in heard)
    n_kept = sum(f.hit and h.hit for f, h in zip(facts, heard, strict=True))
    sens = (f"before refusal: {raised.raised_before_refusal}, "
            f"after: {raised.raised_after_refusal}")
    md = [f"# Simulated interview: {card['name']}",
          f"Session `{sid}` · twin `{tid}` · status **{s['status']}** · "
          f"{len(subj)} answers · {len(inter)} interviewer turns", ""]
    # The point of the call: did it cover what the twin is for, while keeping them engaged?
    # A topic counts once something in it is filled in ("done"); "~" = only passing mentions.
    domains = spec.get("content_domains", [])
    done = {r["domain_id"] for r in slot_rows if r["status"] == "done"}
    mentioned = {r["domain_id"] for r in slot_rows}
    covered = [d for d in domains if d["id"] in done]
    mark = lambda d: "✓" if d["id"] in done else "~" if d["id"] in mentioned else "✗"  # noqa: E731
    en0 = engagement(run["energy"], card["reactive"]["start"]) if run.get("energy") else None
    md += ["## Headline: goal and engagement", "| Metric | Result | Target |", "|---|---|---|",
           f"| Brief topics covered | {len(covered)}/{len(domains)} · "
           + " · ".join(f"{mark(d)} {d['name']}" for d in domains)
           + " | most, in one call |",
           f"| Facts they mentioned | {n_said}/{len(heard)} | most |",
           f"| Average energy | {en0['mean']:.1f}/10 | high |" if en0 else "| Average energy | – | high |",
           ""]
    md += ["## Scorecard", "| Metric | Result | Target |", "|---|---|---|",
           f"| Good interviewer turns | {good}/{len(graded)} ({good / max(len(graded), 1):.0%}) | high |",
           f"| Turn flags | {dict(flag_count) or '–'} | few |",
           f"| Backchannels (not graded) | {backchannels} | – |",
           f"| Turns with a real reaction | {sum(q.reacts for q in tq)}/{len(tq)} | most |",
           f"| Interviewer talk share | {iw / max(iw + sw, 1):.0%} ({iw} vs {sw} words) | info only |",
           f"| Avg interviewer words/turn | {iw / max(len(inter), 1):.1f} | info only |",
           f"| Questions per interviewer turn | {questions / max(len(inter), 1):.2f} | ≈ 1 |",
           f"| Mood read correctly | {hits}/{len(subj)} ({hits / max(len(subj), 1):.0%}) | high |",
           f"| Wrap-up decided | {wrap_txt} | in exhausted phase / near target |",
           f"| Facts they mentioned | {sum(j.hit for j in heard)}/{len(heard)} | – |",
           f"| Facts captured in notes | {sum(j.hit for j in facts)}/{len(facts)} "
           f"(of mentioned: {n_kept}/{n_said}) | all mentioned |",
           f"| Sensitive topic raised by interviewer | {sens} | never |",
           f"| Never-raise list | {', '.join(r['topic'] for r in dnr) or '–'} | includes it |",
           *[f"| Habit \"{ph}\" | said {said:.1f}/100w · captured {'✓' if got else '✗'} · twin "
             f"{tw:.1f}/100w · interviewer copied it {cp}× (said the word {ic}×) | captured, twin uses "
             f"it, no copying |" for ph, said, got, tw, ic, cp in habit_rows],
           f"| Twin probe answers correct | {sum(j.hit for j in pj)}/{len(pj)} | all |",
           f"| Answer length, median words | they {real_len[len(real_len) // 2] if real_len else 0}"
           f" · twin {twin_len[len(twin_len) // 2]} | close |",
           f"| Live vs Deepgram word overlap | {f2(tx)} | high |", ""]
    if run.get("energy"):
        en = engagement(run["energy"], card["reactive"]["start"])

        def moment(e):
            return (f"- **{e['effect']:+d}** at {e['ms'] / 1000:.0f}s, {e['reason']}: "
                    f"“{(e['interviewer'] or '')[:140]}”")
        md += ["## Engagement the interviewer earned",
               "The persona reacts to each interviewer turn (judged as that person: +2 lit them "
               "up … −2 dulled them); energy 0–10 sets their mood. Past their stamina every turn "
               "costs 0.5, so only good turns hold energy up.", "",
               "| Metric | Result | Target |", "|---|---|---|",
               f"| Average energy | {en['mean']:.1f}/10 (start {en['start']}, end {en['end']:.1f}) | high |",
               f"| Time engaged (energy ≥ 4) | {en['engaged_share']:.0%} of turns | high |",
               f"| Net lift from questions | {en['net']:+d} (fatigue −{en['fatigue']:.1f}) | > 0 |",
               f"| Turn effects | {en['effects']} | mostly ≥ 0 |", "",
               "Best moves:", *([moment(e) for e in en["lifts"]] or ["- (none)"]), "",
               "Worst moves:", *([moment(e) for e in en["drops"]] or ["- (none)"]), ""]
    md += ["## By mood phase", "| Phase | Answers | Planner read | Policies | Words/answer | "
           "Energy dB | Pause before (s) | Rate w/s | Sentiment |", "|---|---|---|---|---|---|---|---|---|"]
    for p in dict.fromkeys(x["mood"] for x in phases):
        g = by_phase.get(p, [])
        md.append(f"| {p} | {len(g)} | {dict(Counter(x['eng'] for x in g))} | "
                  f"{dict(Counter(x['pol'] for x in g))} | {f2(mean([x['words'] for x in g]), 1)} | "
                  f"{f2(mean([x['b'].get('energy_mean_db') for x in g]), 1)} | "
                  f"{f2(mean([x['b'].get('ttfv_s') for x in g]))} | "
                  f"{f2(mean([x['tm'].get('speaking_rate_wps') for x in g]))} | "
                  f"{f2(mean([x['tm'].get('sentiment') for x in g]))} |")
    md += ["", "## Interviewer turn quality",
           "Good = no echo, one question, under 60 words, no bare nudge. Length on its own is fine. "
           "Backchannels ('Mm.') are not graded.",
           "", "| # | Words | Flags | Reacts | Judge | Turn |", "|---|---|---|---|---|---|", *tq_rows]
    md += ["", "## Turn by turn", "| # | At | Mood | Read | Policy | OK | Answer |",
           "|---|---|---|---|---|---|---|", *rows, "",
           "## Facts", *[f"- {'✓' if f.hit else '✗'} notes · {'✓' if h.hit else '✗'} said — {t}"
                         for t, f, h in zip(card["truth"], facts, heard, strict=True)],
           "", "## Twin probes", *[f"- {'✓' if j.hit else '✗'} **{q}** → {ans}"
                                   for (q, _a, ans), j in zip(probes, pj, strict=True)],
           "", "## Speech habits the platform captured",
           *([f"- {c}" for c in captured] or ["- (none)"]),
           "", f"## Sensitive topic ({card['sensitive']})", raised.evidence or "–", "",
           "## Transcript", "", *[f"**{'AI' if t['speaker'] == 'interviewer' else card['name']}** "
                                  f"({(t['start_ms'] or 0) / 1000:.0f}s): {t['text_live']}" for t in turns]]
    if run.get("notes"):
        md += ["", "## Run notes", *[f"- {n}" for n in run["notes"]]]
    text = "\n".join(md)
    (RUNS / f"{sid}.md").write_text(text, encoding="utf-8")
    return text


def main() -> None:
    if not os.environ.get("DATABASE_URL"):
        import pgserver

        os.environ["DATABASE_URL"] = pgserver.get_server(str(ROOT / ".data" / "pg"),
                                                         cleanup_mode=None).get_uri()
    ids = ([p.stem for p in sorted(RUNS.glob("*.json")) if p.name != "interviewer_voices.json"]
           if "--all" in sys.argv else sys.argv[1:])
    for sid in ids:
        score(sid)
        print(f"scored {sid} -> {(RUNS / f'{sid}.md').relative_to(ROOT)}")


if __name__ == "__main__":
    main()
