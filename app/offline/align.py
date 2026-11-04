"""Merge overlapping segment transcripts and align words to turns (plan 7.9). Pure functions."""

from __future__ import annotations

from difflib import SequenceMatcher

TOLERANCE_MS = 300


def merge_segments(segments: list[dict]) -> list[list]:
    """`segments` = [{"start_ms", "end_ms", "words": [[w, s, e], ...]}] sorted by start.

    In an overlap, a word is kept from the segment where it sits further from the edge:
    the cut is the overlap's midpoint.
    """
    segments = sorted(segments, key=lambda s: s["start_ms"])
    merged: list[list] = []
    for i, seg in enumerate(segments):
        lo = float("-inf")
        hi = float("inf")
        if i > 0:
            prev = segments[i - 1]
            if prev.get("end_ms") is not None and prev["end_ms"] > seg["start_ms"]:
                lo = (seg["start_ms"] + prev["end_ms"]) / 2
        if i + 1 < len(segments):
            nxt = segments[i + 1]
            if seg.get("end_ms") is not None and seg["end_ms"] > nxt["start_ms"]:
                hi = (nxt["start_ms"] + seg["end_ms"]) / 2
        for w in seg["words"]:
            mid = (w[1] + w[2]) / 2
            if lo <= mid < hi:
                merged.append(w)
    merged.sort(key=lambda w: w[1])
    return merged


def _norm(token: str) -> str:
    return token.lower().strip(".,!?…;:'\"-—")


ECHO_DRIFT_MS = 1500


def echo_indices(words: list[list], turn: dict) -> set[int]:
    """Indices of `words` that are the interviewer's own voice picked up by the mic.

    Echo repeats the interviewer's text in order and at about the time it was spoken, so a
    word counts only if it's part of an in-order run of 2+ matching tokens (or one long word)
    and sits near where that token falls when the turn's text is spread over its duration.
    A subject who barges in with similar words at a different moment is kept.
    """
    tokens = [_norm(x) for x in (turn.get("text") or "").split()]
    if not tokens:
        return set()
    lo, hi = turn["start_ms"] - TOLERANCE_MS, turn["end_ms"] + TOLERANCE_MS
    idx = [i for i, w in enumerate(words) if lo <= (w[1] + w[2]) / 2 <= hi]
    seq = [_norm(words[i][0]) for i in idx]
    span = max(turn["end_ms"] - turn["start_ms"], 1)
    echo: set[int] = set()
    for a, b, n in SequenceMatcher(None, seq, tokens, autojunk=False).get_matching_blocks():
        if n == 0 or (n == 1 and len(seq[a]) <= 4):
            continue
        for k in range(n):
            w = words[idx[a + k]]
            expected = turn["start_ms"] + span * (b + k) / len(tokens)
            if abs(w[1] - expected) <= ECHO_DRIFT_MS:
                echo.add(idx[a + k])
    return echo


def align(words: list[list], turns: list[dict]) -> dict[str, list[list]]:
    """`turns` = [{"id", "speaker", "start_ms", "end_ms", "text"?}]. Returns subject turn id →
    words.

    A word that falls inside an interviewer turn's time span is echo (the AI's voice picked up
    by the mic) when it matches that turn's text, and is dropped. Otherwise it's the subject
    talking over the interviewer (barge-in) and is kept. Words inside an interviewer span but
    outside every subject turn are always dropped. Kept words go to the subject turn that
    contains them (± tolerance), else the nearest one.
    """
    subj = [t for t in turns if t["speaker"] == "subject" and t.get("start_ms") is not None]
    inter = [t for t in turns if t["speaker"] == "interviewer" and t.get("start_ms") is not None
             and t.get("end_ms") is not None]
    echo = set().union(*(echo_indices(words, t) for t in inter)) if inter else set()
    out: dict[str, list[list]] = {str(t["id"]): [] for t in subj}
    if not subj:
        return out
    for i, w in enumerate(words):
        if i in echo:
            continue
        mid = (w[1] + w[2]) / 2
        over = [t for t in inter if t["start_ms"] <= mid <= t["end_ms"]]
        home = None
        for t in subj:
            end = t["end_ms"] if t.get("end_ms") is not None else t["start_ms"]
            if t["start_ms"] - TOLERANCE_MS <= mid <= end + TOLERANCE_MS:
                home = t
                break
        if home is None:
            if over:
                continue
            home = min(subj, key=lambda t: min(abs(mid - t["start_ms"]),
                                               abs(mid - (t.get("end_ms") or t["start_ms"]))))
        out[str(home["id"])].append(w)
    return out
