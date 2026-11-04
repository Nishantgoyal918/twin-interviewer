"""Offline voice features from Deepgram word timestamps. Pure Python, no audio decoding."""

from __future__ import annotations

import re

FILLERS = {"um", "uh", "umm", "uhh", "hmm", "erm", "er", "ah", "mm", "matlab", "haan", "accha"}
DEVANAGARI = re.compile(r"[ऀ-ॿ]")
PAUSE_MS = 250


def _script(word: str) -> str:
    return "deva" if DEVANAGARI.search(word) else "latin"


def timing_features(words: list[list], fillers_reliable: bool = True) -> dict[str, float]:
    """`words` = [[text, start_ms, end_ms(, sentiment)], ...] for one subject turn, in order.
    `fillers_reliable` is False for multi-language transcripts, which drop fillers."""
    if not words:
        return {}
    n = len(words)
    span_s = max((words[-1][2] - words[0][1]) / 1000, 0.001)
    gaps = [b[1] - a[2] for a, b in zip(words, words[1:], strict=False)]
    pauses = [g for g in gaps if g >= PAUSE_MS]
    tokens = [w[0].lower().strip(".,!?…") for w in words]
    scripts = [_script(w[0]) for w in words]
    switches = sum(1 for a, b in zip(scripts, scripts[1:], strict=False) if a != b)
    out = {
        "speaking_rate_wps": n / span_s,
        "pause_count": float(len(pauses)),
        "pause_mean_s": (sum(pauses) / len(pauses) / 1000) if pauses else 0.0,
        "filler_rate": sum(1 for t in tokens if t in FILLERS) / n,
        "code_switch_rate": switches / max(n - 1, 1),
        "turn_words": float(n),
    }
    if not fillers_reliable:
        del out["filler_rate"]
    sentiments = [w[3] for w in words if len(w) > 3]
    if sentiments:
        out["sentiment"] = sum(sentiments) / len(sentiments)
    return out
