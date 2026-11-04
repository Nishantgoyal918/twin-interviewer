"""Conversationalist (plan 7.2): instructions builder shared by text and voice, the text
agent, and the GPT-Live session config."""

from __future__ import annotations

import httpx

from app.llm import llm, prompt
from app.models import TwinSpec
from app.settings import get_settings

FIRST_SESSION = (
    "## This is your first conversation with them\n"
    "Open by saying, in one or two short sentences, that you're an AI interviewer and that "
    "this chat helps build a twin of them for {audience}. Then ask one easy, warm question."
)
RETURNING = (
    "## You've talked before\n"
    "Open with one short line that picks up a detail from your notes, then one question."
)

ONGOING = (
    "## The chat is under way\n"
    "You already introduced yourself. Never introduce yourself or explain the project again; "
    "just continue the conversation."
)

LANG_NAMES = {"hi": "Hindi", "en": "English", "mr": "Marathi", "ta": "Tamil", "bn": "Bengali",
              "gu": "Gujarati", "pa": "Punjabi", "te": "Telugu", "ur": "Urdu"}


def languages_line(spec: TwinSpec) -> str:
    names = [LANG_NAMES.get(code, code) for code in spec.subject.languages] or ["English"]
    if spec.subject.code_switching and len(names) > 1:
        return (f"They mix {' and '.join(names)}. Mostly use the language they use, keep "
                "your own mixing light; never imitate their mixing pattern.")
    return f"Speak {names[0]} unless they switch."


def build_instructions(spec: TwinSpec, *, notes: str, do_not_raise: list[str], guidance: str,
                       first_session: bool, modality: str, opening: bool = True) -> str:
    text = prompt("conversationalist_rules").format(
        subject_name=spec.subject.name,
        purpose=spec.purpose,
        audience=spec.audience,
        stance=spec.interviewer_stance,
        languages_line=languages_line(spec),
        first_session_block=(ONGOING if not opening
                             else FIRST_SESSION.format(audience=spec.audience) if first_session
                             else RETURNING),
        notes=notes or "(nothing yet)",
        do_not_raise=", ".join(do_not_raise) or "(none)",
        guidance=guidance or "Get to know them; follow their lead.",
    )
    if modality == "text":
        text += ("\n\n## Text chat\nWrite like a person texting: short, plain, no lists, no "
                 "emoji unless they use them. Reply with your message only.")
    else:
        text += ("\n\n## Voice\nSpeak briefly. Use few, sparse backchannels (\"mm\", \"okay\") "
                 "only at natural pauses. Wait through pauses of several seconds; slow "
                 "speakers need time. When the app hands you notes, use them naturally.")
    return text


def text_reply(spec: TwinSpec, *, transcript: str, notes: str, do_not_raise: list[str],
               guidance: str, first_session: bool, opening: bool = False) -> str:
    instructions = build_instructions(spec, notes=notes, do_not_raise=do_not_raise,
                                      guidance=guidance, first_session=first_session,
                                      modality="text", opening=opening)
    task_input = (transcript + "\n\nInterviewer:") if transcript else (
        "(The chat is starting now. Write your opening message.)" if opening else "")
    return llm().text("text_agent", instructions, task_input)


# ---- GPT-Live (plan 5.1 / 7.10) -------------------------------------------------------------

VOICE_GREETING = ("Start now: greet them and open the conversation exactly as your "
                  "instructions say, in one or two short sentences. Then stop and listen.")


class VoiceNotConfigured(RuntimeError):
    pass


def create_live_session(spec: TwinSpec, *, sdp_offer: str, notes: str, do_not_raise: list[str],
                        guidance: str, first_session: bool, voice: str | None = None) -> dict:
    """The interviewer's GPT-Live session for the browser's SDP offer → {"id", "sdp"}."""
    instructions = build_instructions(spec, notes=notes, do_not_raise=do_not_raise,
                                      guidance=guidance, first_session=first_session,
                                      modality="voice")
    return open_live(instructions, sdp_offer, voice or get_settings().cfg("voice.voice", "marin"),
                     greeting=VOICE_GREETING)


def open_live(instructions: str, sdp_offer: str, voice: str, greeting: str | None = None) -> dict:
    """POST /v1/live/sessions (WebRTC). `greeting` makes the model speak first."""
    s = get_settings()
    if not s.openai_api_key:
        raise VoiceNotConfigured("Voice needs OPENAI_API_KEY.")
    session: dict = {
        "model": s.cfg("voice.model", "gpt-live-1"),
        "instructions": instructions,
        "audio": {"output": {"voice": voice}},
        "delegation": {"type": "client",
                       "acknowledgement_delay_ms": s.cfg("voice.acknowledgement_delay_ms", 800)},
    }
    if greeting:
        # GPT-Live only speaks first when told to in a developer message at creation.
        session["input"] = [{"type": "message", "role": "developer",
                             "content": [{"type": "input_text", "text": greeting}]}]
    r = httpx.post("https://api.openai.com/v1/live/sessions",
                   headers={"Authorization": f"Bearer {s.openai_api_key}"},
                   json={"session": session, "transport": {"type": "webrtc", "sdp": sdp_offer}},
                   timeout=30)
    if r.status_code >= 400:
        raise RuntimeError(f"GPT-Live session create failed: {r.status_code} {r.text[:300]}")
    data = r.json()
    return {"id": data["session"]["id"], "sdp": data["transport"]["sdp"]}
