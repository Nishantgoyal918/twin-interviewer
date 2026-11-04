"""Synthetic voice cases through the real offline path: TTS → Deepgram → words → timing features
→ tags against a neutral baseline → observer → planner turn state. Also checks segment
overlap merging and echo/barge-in alignment.

Speech comes from Deepgram Aura (plain voice) and OpenAI gpt-4o-mini-tts (style instructions
for emotion, Hinglish). Needs DEEPGRAM_API_KEY and OPENAI_API_KEY.

    uv run python scripts/audio_cases.py [--keep DIR]
"""

from __future__ import annotations

import array
import io
import json
import sys
import wave
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.fakes import fake_spec  # noqa: E402
from app.models import TwinSpec  # noqa: E402
from app.offline import deepgram as dg  # noqa: E402
from app.offline.align import align, merge_segments  # noqa: E402
from app.planner import policy  # noqa: E402
from app.settings import get_settings  # noqa: E402
from app.signals import baseline  # noqa: E402
from app.signals.tags import make_tags  # noqa: E402
from app.signals.timing_features import timing_features  # noqa: E402

RATE = 24000
KEEP = Path(sys.argv[sys.argv.index("--keep") + 1]) if "--keep" in sys.argv else None


# ---- audio helpers (16-bit mono PCM at 24 kHz) ----------------------------------------------

def aura(text: str, voice: str = "aura-2-thalia-en") -> array.array:
    r = httpx.post("https://api.deepgram.com/v1/speak",
                   params={"model": voice, "encoding": "linear16", "sample_rate": RATE, "container": "none"},
                   headers={"Authorization": f"Token {get_settings().deepgram_api_key}"},
                   json={"text": text}, timeout=120)
    r.raise_for_status()
    return array.array("h", r.content)


def openai_tts(text: str, style: str, voice: str = "coral") -> array.array:
    from openai import OpenAI

    client = OpenAI(api_key=get_settings().openai_api_key)
    resp = client.audio.speech.create(model="gpt-4o-mini-tts", voice=voice, input=text,
                                      instructions=style, response_format="pcm")
    return array.array("h", resp.content)


def silence(s: float) -> array.array:
    return array.array("h", bytes(int(s * RATE) * 2))


def cat(*parts: array.array) -> array.array:
    out = array.array("h")
    for p in parts:
        out.extend(p)
    return out


def mix(a: array.array, b: array.array, b_at_s: float, b_gain: float = 0.6) -> array.array:
    """b laid over a starting at b_at_s (b quieter, like speaker echo in the mic)."""
    off = int(b_at_s * RATE)
    out = array.array("h", a)
    if len(out) < off + len(b):
        out.extend(bytes((off + len(b) - len(out)) * 2))
    for i, v in enumerate(b):
        out[off + i] = max(-32768, min(32767, out[off + i] + int(v * b_gain)))
    return out


def secs(a: array.array) -> float:
    return len(a) / RATE


def wav(a: array.array, start_s: float = 0, end_s: float | None = None) -> bytes:
    pcm = a[int(start_s * RATE): int(end_s * RATE) if end_s else None]
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(pcm.tobytes())
    return buf.getvalue()


def transcribe(a: array.array, start_s: float = 0, end_s: float | None = None, extra=None) -> dict:
    return dg.transcribe_now(wav(a, start_s, end_s), "audio/wav", extra)


# ---- cases ----------------------------------------------------------------------------------

NEUTRAL = [
    "We lived near the old market. My father had a small cloth shop there, and I helped after school.",
    "On Sundays my aunt cooked for everyone. She made dal, rice, and sometimes kheer if there was milk.",
    "I studied in a girls' school until tenth class. Then I went to college in the city by bus.",
]

STYLES = {
    "excited": ("Oh, the Diwali at my nani's house was the best! Everyone came, we made sweets all "
                "night, and the kids ran on the roof with sparklers!",
                "Speak fast, excited, joyful, laughing a little, high energy."),
    "tired": ("I don't know. It was a long time ago. We just. Went there. That's all.",
              "Speak slowly and flatly, tired and bored, with long pauses between phrases."),
    "sad_hesitant": ("Um... my mother, she... uh, she was ill for a long time. I, I don't... "
                     "I don't really remember much of that year.",
                     "Speak quietly and hesitantly, sad, with ums and long pauses, voice breaking a bit."),
    "hinglish": ("Mujhe yaad hai, every Sunday we went to the bazaar, aur wahan pe garam kachori "
                 "milti thi. Mummy kehti thi, zyada mat khao.",
                 "Speak naturally like a North Indian woman mixing Hindi and English, warm tone."),
}


def features_for(a: array.array, lead_s: float, extra=None) -> tuple[dict, list]:
    words = dg.words_from_response(transcribe(a, extra=extra))
    f = timing_features(words)
    f["ttfv_s"] = round(words[0][1] / 1000, 2) if words else None
    return f, words


def observe(text: str) -> dict:
    from app.observer import observe as run

    spec = TwinSpec.model_validate(fake_spec("a twin of my mother"))
    out, ms = run(spec, f"INTERVIEWER: Tell me about that.\nSUBJECT: {text}", "", {})
    return {"lexical": out.lexical_engagement, "deflection": out.deflection,
            "register": out.talk_register, "ms": ms}


def main() -> None:
    report: dict = {"cases": {}}
    stats: dict = {}

    print("— neutral baseline (Aura) —")
    for i, text in enumerate(NEUTRAL):
        a = cat(silence(0.6), aura(text))
        f, _ = features_for(a, 0)
        stats = baseline.update(stats, {k: v for k, v in f.items() if isinstance(v, (int, float))})
        print(f"  n{i}: rate={f['speaking_rate_wps']:.2f} pauses={f['pause_count']} ttfv={f['ttfv_s']}")
    report["baseline"] = {k: {"mean": round(v["mean"], 3), "std": round(baseline.std(v), 3)}
                          for k, v in stats.items()}

    cases = {name: (openai_tts(text, style), text) for name, (text, style) in STYLES.items()}
    cases["long_pause_before"] = (cat(silence(3.5), aura("Hmm. Yes, I remember that house.")), None)
    cases["flat_short"] = (cat(silence(0.8), aura("Yes.")), None)
    for name, (pcm, _text) in cases.items():
        if not name.startswith(("long", "flat")):
            pcm = cat(silence(0.6), pcm)
        if KEEP:
            KEEP.mkdir(parents=True, exist_ok=True)
            (KEEP / f"{name}.wav").write_bytes(wav(pcm))
        f, words = features_for(pcm, 0)
        heard = " ".join(w[0] for w in words)
        tags = make_tags({k: v for k, v in f.items() if v is not None}, twin_stats=stats, session_stats={})
        obs = observe(heard) if heard else {}
        from app.models import ObserverOutput

        o = ObserverOutput.model_validate({
            "facts": [], "topics": [], "register": obs.get("register", "reacting"),
            "new_interests": [], "deflection": obs.get("deflection", False), "deflected_topic": None,
            "lexical_engagement": obs.get("lexical", "unclear"), "slot_updates": []})
        state = policy.turn_state(o, [t for t in tags if t.confidence >= 0.5], len(words), [])
        report["cases"][name] = {"heard": heard, "features": f,
                                 "tags": [(t.tag, t.value, t.confidence) for t in tags],
                                 "observer": obs, "turn_state": state}
        print(f"\n— {name} —\n  heard: {heard}\n  {json.dumps(f)}\n"
              f"  tags: {[(t.tag, t.value) for t in tags]}\n  observer: {obs}\n  → {state}")

    # Segment overlap: one recording cut in two with 2 s overlap must equal the whole.
    print("\n— segment overlap —")
    long = cat(silence(0.5), aura(NEUTRAL[0] + " " + NEUTRAL[1] + " " + NEUTRAL[2]))
    total = secs(long)
    cut = total / 2
    whole = dg.words_from_response(transcribe(long))
    s1 = {"start_ms": 0, "end_ms": int((cut + 1) * 1000),
          "words": dg.words_from_response(transcribe(long, 0, cut + 1))}
    s2 = {"start_ms": int((cut - 1) * 1000), "end_ms": int(total * 1000),
          "words": dg.words_from_response(transcribe(long, cut - 1, None), int((cut - 1) * 1000))}
    merged = merge_segments([s1, s2])
    norm = lambda ws: [w[0].lower().strip(".,!?") for w in ws]  # noqa: E731
    ok = norm(merged) == norm(whole)
    report["segments"] = {"whole_words": len(whole), "merged_words": len(merged), "identical": ok}
    print(f"  whole={len(whole)} merged={len(merged)} identical={ok}")
    if not ok:
        print("  whole :", " ".join(norm(whole)))
        print("  merged:", " ".join(norm(merged)))

    # Echo + barge-in: interviewer (Aura voice) plays through speakers, subject cuts in.
    print("\n— echo / barge-in —")
    q = aura("So tell me, who taught you to cook, and what was the first dish you made?",
             "aura-2-orion-en")
    ans = openai_tts("My aunt Kamla! The first thing was aloo paratha, and I burned it.",
                     "Speak eagerly, interrupting.")
    q_end = secs(q)
    barge_at = q_end - 1.5                       # subject starts 1.5 s before the question ends
    mic = mix(cat(silence(barge_at), ans), q, 0, b_gain=0.5)
    words = dg.words_from_response(transcribe(mic))
    turns = [
        {"id": "q", "speaker": "interviewer", "start_ms": 0, "end_ms": int(q_end * 1000),
         "text": "So tell me, who taught you to cook, and what was the first dish you made?"},
        {"id": "a", "speaker": "subject", "start_ms": int(barge_at * 1000),
         "end_ms": int((barge_at + secs(ans)) * 1000)},
    ]
    kept = " ".join(w[0] for w in align(words, turns).get("a", []))
    diar = transcribe(mic, extra=dg.params() + [("diarize", "true")])
    spk = diar["results"]["channels"][0]["alternatives"][0]["words"]
    by_spk: dict = {}
    for w in spk:
        by_spk.setdefault(w.get("speaker"), []).append(w.get("punctuated_word") or w["word"])
    report["barge_in"] = {"heard": " ".join(w[0] for w in words), "kept_for_subject": kept,
                          "diarized": {str(k): " ".join(v) for k, v in by_spk.items()}}
    print("  heard   :", report["barge_in"]["heard"])
    print("  aligned :", kept)
    for k, v in by_spk.items():
        print(f"  speaker {k}:", " ".join(v))

    out = ROOT / ".data" / "audio_cases.json"
    out.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nreport: {out}")


if __name__ == "__main__":
    main()
