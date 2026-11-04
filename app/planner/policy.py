"""Engagement state per turn and over a trend (plan 7.6 steps 2–3)."""

from __future__ import annotations

from collections import Counter

from app.models import Engagement, ObserverOutput, Tag
from app.settings import get_settings

LEX = {"high": 2.0, "medium": 1.0, "low": -1.0, "unclear": 0.0}
FRAGMENT_WORDS = 8
POSITIVE_UP = {"reply_words", "energy_mean_db", "voiced_s", "laughter", "questions_back",
               "burst_count", "speaking_rate_wps"}


def turn_state(obs: ObserverOutput, tags: list[Tag], word_count: int,
               prev_counts: list[int]) -> Engagement:
    if obs.deflection:
        return "avoidant"
    if obs.lexical_engagement == "unclear" and word_count < FRAGMENT_WORDS:
        # A cut-off fragment ("Like, the whole") or a "You too": its pitch and length say
        # nothing about engagement, and counting it as short made engaged people read flat.
        return "uncertain"
    score = LEX[obs.lexical_engagement]
    evidence = obs.lexical_engagement != "unclear"
    for t in tags:
        if t.tag == "long_pause_before_answer":
            score -= 0.5
            evidence = True
        elif t.tag in POSITIVE_UP and t.z is not None:
            score += 0.5 if t.z > 0 else -0.5
            evidence = True
    recent = prev_counts[-3:]
    if len(recent) >= 2 and word_count < 0.5 * (sum(recent) / len(recent)):
        score -= 1
        evidence = True
    if not evidence:
        return "uncertain"
    if score >= 2:
        return "engaged"
    if score <= -1:
        return "flat"
    return "uncertain"


def trend_state(trend: list[str], elapsed_s: float, target_minutes: int) -> Engagement:
    """Acts on a trend, never on one turn — except an explicit deflection (topic drop)."""
    cfg = get_settings().cfg
    window = int(cfg("signals.trend_window", 3))
    need = int(cfg("signals.trend_min", 2))
    if trend and trend[-1] == "avoidant":
        return "avoidant"
    last = [s for s in trend[-window:] if s != "uncertain"]
    common = Counter(last).most_common(1)
    state: Engagement = "uncertain"
    if common and common[0][1] >= need:
        state = common[0][0]  # type: ignore[assignment]
    if state == "flat" and trend and trend[-1] == "engaged":
        # Recover at once: one clearly engaged answer ends a flat stretch. Wrongly reading
        # "engaged" costs little; nagging a warmed-up person with "change_kind" costs a lot.
        state = "engaged"
    target_s = target_minutes * 60
    # Ending early can't be undone, so a late flat stretch must be solid: the last `window`
    # readable answers all flat. A dip of one or two short replies doesn't end the chat.
    solid_flat = len(last) >= window and all(s == "flat" for s in last)
    if elapsed_s >= target_s or (elapsed_s >= 0.7 * target_s and solid_flat):
        return "tired"
    return state
