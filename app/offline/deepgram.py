"""Deepgram pre-recorded API with async callback (plan 5.3)."""

from __future__ import annotations

import httpx

from app.settings import get_settings

LISTEN_URL = "https://api.deepgram.com/v1/listen"


class DeepgramNotConfigured(RuntimeError):
    pass


def single_language(languages: list[str] | None) -> str | None:
    """The one language to transcribe in, or None when the subject mixes languages."""
    langs = sorted(set(languages or []))
    return langs[0] if len(langs) == 1 else None


def params(languages: list[str] | None = None) -> list[tuple[str, str]]:
    """Language follows the subject. Measured on synthetic speech (scripts/audio_cases.py):
    `multi` keeps Hinglish (Devanagari) but drops fillers; `en` keeps "um"/"uh" and adds
    sentiment but loses Hindi. So: one language → that language (+ sentiment for English);
    several → multi with hints, and filler counts are treated as unavailable."""
    cfg = get_settings().cfg
    p = [("model", cfg("deepgram.model", "nova-3")),
         ("punctuate", str(cfg("deepgram.punctuate", True)).lower()),
         ("filler_words", str(cfg("deepgram.filler_words", True)).lower()),
         ("smart_format", "false")]
    one = single_language(languages)
    if one:
        p.append(("language", one))
        if one == "en" and cfg("deepgram.sentiment", True):
            p.append(("sentiment", "true"))
        return p
    p.append(("language", cfg("deepgram.language", "multi")))
    for hint in languages or cfg("deepgram.language_hints", []) or []:
        p.append(("language_hint", hint))
    return p


def submit(audio_url: str, callback_url: str, languages: list[str] | None = None) -> str:
    """Queues a transcription; Deepgram POSTs the result to callback_url. Returns request id."""
    key = get_settings().deepgram_api_key
    if not key:
        raise DeepgramNotConfigured("DEEPGRAM_API_KEY is not set")
    r = httpx.post(LISTEN_URL, params=[*params(languages), ("callback", callback_url)],
                   headers={"Authorization": f"Token {key}"}, json={"url": audio_url}, timeout=30)
    r.raise_for_status()
    return r.json().get("request_id", "")


def transcribe_now(audio: bytes, mimetype: str = "audio/webm", extra: list | None = None) -> dict:
    """Synchronous call, used by the ASR benchmark script."""
    key = get_settings().deepgram_api_key
    if not key:
        raise DeepgramNotConfigured("DEEPGRAM_API_KEY is not set")
    r = httpx.post(LISTEN_URL, params=extra or params(),
                   headers={"Authorization": f"Token {key}", "content-type": mimetype},
                   content=audio, timeout=300)
    r.raise_for_status()
    return r.json()


def words_from_response(body: dict, offset_ms: int = 0) -> list[list]:
    """→ [[word, start_ms, end_ms(, sentiment_score)], ...] on the session clock.
    Shape verified against nova-3 responses (2026-11-02)."""
    try:
        words = body["results"]["channels"][0]["alternatives"][0]["words"]
    except (KeyError, IndexError):
        return []
    out = []
    for w in words:
        text = w.get("punctuated_word") or w.get("word") or ""
        row = [text, int(w["start"] * 1000) + offset_ms, int(w["end"] * 1000) + offset_ms]
        if "sentiment_score" in w:
            row.append(round(float(w["sentiment_score"]), 3))
        out.append(row)
    return out
