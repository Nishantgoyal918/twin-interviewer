"""Per-register style stats and the stability check for "register done" (plan 7.7)."""

from __future__ import annotations

import re

from pydantic import BaseModel

from app.llm import llm, prompt
from app.signals.timing_features import FILLERS

SENT = re.compile(r"[.!?…]+|\n")
WORD = re.compile(r"\w+", re.UNICODE)


def style_stats(texts: list[str]) -> dict[str, float]:
    words = [w.lower() for t in texts for w in WORD.findall(t)]
    if not words:
        return {}
    sentences = [s for t in texts for s in SENT.split(t) if WORD.search(s)]
    head = words[:300]
    return {
        "words_per_sentence": len(words) / max(len(sentences), 1),
        "filler_per_100": 100 * sum(1 for w in words if w in FILLERS) / len(words),
        "type_token_ratio": len(set(head)) / len(head),
    }


def is_stable(history: list[dict], k: int, tolerance: float) -> bool:
    """Stable when the last k sessions' stats are each within `tolerance` of their mean."""
    recent = [h["stats"] for h in history if h.get("stats")][-k:]
    if len(recent) < k:
        return False
    for key in recent[0]:
        vals = [r.get(key) for r in recent if r.get(key) is not None]
        if len(vals) < k:
            return False
        mean = sum(vals) / len(vals)
        scale = max(abs(mean), 1e-6)
        if any(abs(v - mean) / scale > tolerance for v in vals):
            return False
    return True


# ---- speech habits (signature phrases: "like", "correct?", "basically", "na") ----------------

class Habit(BaseModel):
    phrase: str
    use: str  # how they use it, e.g. "tag at the end of a statement to check you agree"


class Example(BaseModel):
    index: int
    why: str


class Habits(BaseModel):
    items: list[Habit]
    examples: list[Example] = []


def phrase_hits(text: str, phrase: str) -> int:
    return len(re.findall(rf"(?<!\w){re.escape(phrase.lower())}(?!\w)", text.lower()))


MIN_EXAMPLE_WORDS = 15
MIN_EXAMPLES, MAX_EXAMPLES = 4, 5
# the twin would copy these as if they were who the person is, not how they felt that day
PASSING_MOOD = re.compile(r"\b(tired|exhausted|wiped|sleepy|drained|bored|wrap up|stop now)\b",
                          re.I)


def voice_profile(pairs: list[dict], min_count: int = 3, min_turns: int = 2
                  ) -> tuple[list[dict], list[dict]]:
    """One pass over everything they've said → (speech habits, voice examples).

    `pairs` are {"turn_id", "question", "answer", "low_energy"}. The LLM proposes habits and
    picks the answers that best show how they talk; plain code keeps only habits that really
    repeat and examples that are full answers given with normal energy."""
    pairs = [p for p in pairs if (p.get("answer") or "").strip()]
    answers = [p["answer"] for p in pairs]
    total = sum(len(WORD.findall(t)) for t in answers)
    if total < 80:
        return [], []
    # most recent exchanges that fit, numbered so the model can point at them
    shown, used = [], 0
    for i in range(len(pairs) - 1, -1, -1):
        block = f"{i}. Q: {pairs[i].get('question') or '(none)'}\n   A: {pairs[i]['answer']}"
        if used + len(block) > 12000:
            break
        shown.append(block)
        used += len(block)
    found = llm().json("speech_habits", prompt("speech_habits"), "\n".join(reversed(shown)), Habits)

    habits = []
    for h in found.items:
        phrase = h.phrase.strip().strip("\"'.,?!").lower()
        if not phrase:
            continue
        count = sum(phrase_hits(t, phrase) for t in answers)
        turns = sum(1 for t in answers if phrase_hits(t, phrase))
        if count >= min_count and turns >= min_turns:
            habits.append({"phrase": phrase, "use": h.use.strip(), "count": count,
                           "per_100_words": round(100 * count / total, 1)})
    habits = sorted(habits, key=lambda h: -h["per_100_words"])[:8]

    def eligible(i: int) -> bool:
        if not 0 <= i < len(pairs) or pairs[i].get("low_energy"):
            return False
        a = pairs[i]["answer"].strip()
        whole = a[0].isupper() or a[0] in "\"'"  # starting mid-word means a split turn
        return (whole and len(WORD.findall(a)) >= MIN_EXAMPLE_WORDS
                and not PASSING_MOOD.search(a))

    picked = list(dict.fromkeys(e.index for e in found.examples if eligible(e.index)))
    why = {e.index: e.why.strip() for e in found.examples}
    if len(picked) < MIN_EXAMPLES:  # top up with the answers densest in their habits
        def density(i: int) -> float:
            a = pairs[i]["answer"]
            return sum(phrase_hits(a, h["phrase"]) for h in habits) / max(len(WORD.findall(a)), 1)
        rest = sorted((i for i in range(len(pairs)) if eligible(i) and i not in picked),
                      key=density, reverse=True)
        picked += rest[:MIN_EXAMPLES - len(picked)]
    examples = [{**{k: pairs[i].get(k) for k in ("turn_id", "question", "answer")},
                 "why": why.get(i, "")} for i in picked[:MAX_EXAMPLES]]
    return habits, examples


def speech_habits(texts: list[str], min_count: int = 3, min_turns: int = 2) -> list[dict]:
    """Habits only, from plain answers (no questions)."""
    return voice_profile([{"answer": t} for t in texts], min_count, min_turns)[0]


def habit_line(h: dict) -> str:
    return f"Says \"{h['phrase']}\" a lot ({h['per_100_words']} per 100 words): {h['use']}"
