"""Text-chat features per subject turn. Never uses cross-session latency (plan 7.5)."""

from __future__ import annotations

import re

LAUGH = re.compile(r"\b(ha(ha)+|he(he)+|lol+|lmao|rofl)\b|😂|🤣|😆|😄", re.I)
EMOJI = re.compile("[\U0001F300-\U0001FAFF☀-➿]")
WORD = re.compile(r"\w+", re.UNICODE)


def text_features(text: str, messages: list[dict] | None = None) -> dict[str, float]:
    """`messages` = the burst as sent: [{"text", "at_ms"}], at_ms relative to the prompt."""
    words = WORD.findall(text)
    msgs = messages or [{"text": text}]
    feats: dict[str, float] = {
        "reply_words": float(len(words)),
        "burst_count": float(len(msgs)),
        "questions_back": float(text.count("?")),
        "laughter": float(len(LAUGH.findall(text))),
        "emoji": float(len(EMOJI.findall(text))),
    }
    times = [m.get("at_ms") for m in msgs if isinstance(m.get("at_ms"), int | float)]
    if len(times) >= 2:
        gaps = [b - a for a, b in zip(times, times[1:], strict=False)]
        feats["within_burst_latency_s"] = sum(gaps) / len(gaps) / 1000
    if times:
        feats["first_reply_latency_s"] = times[0] / 1000
    return feats
