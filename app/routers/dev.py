"""Simulator API. A persona agent, a second GPT-Live session playing a person from a card,
takes a real voice interview on the platform. See docs/simulator.md.

Mounted when DEV_AUTH=1 (local dev), or when SIM_KEY is set (e.g. on the deployed app), where
every route needs that key (`x-sim-key` header or `?key=`): persona sessions cost money and
aren't covered by the spend caps. Runs are kept on local disk in dev and in object storage
(sim/...) elsewhere, since serverless functions have no lasting disk."""

from __future__ import annotations

import hmac
import json
from pathlib import Path

import yaml
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import FileResponse, PlainTextResponse, RedirectResponse
from pydantic import BaseModel, Field

from app import db
from app.conversation import open_live
from app.routers import creator
from app.security.auth import SHARED_CREATOR
from app.settings import ROOT, get_settings
from app.storage import storage


def require_key(request: Request, x_sim_key: str | None = Header(default=None)) -> None:
    key = get_settings().sim_key
    if not key:
        return  # local dev (DEV_AUTH) only: no key configured
    given = x_sim_key or request.query_params.get("key") or ""
    if not hmac.compare_digest(given, key):
        raise HTTPException(404, "Not Found")


router = APIRouter(prefix="/api/dev", dependencies=[Depends(require_key)])

PERSONAS = ROOT / "personas"
RUNS = ROOT / ".data" / "sim"
SIM_CREATOR = "00000000-0000-0000-0000-0000000051a0"
PUBLIC_KEYS = ("id", "name", "voice", "interviewer_voice", "run_seconds", "phases", "reactive")


VOICES = RUNS / "interviewer_voices.json"


# ---- run store: local files in dev, object storage when deployed ----------------------------
def _local() -> bool:
    return get_settings().storage_backend == "local"


def _put(name: str, data: bytes, content_type: str) -> None:
    if _local():
        RUNS.mkdir(parents=True, exist_ok=True)
        (RUNS / name).write_bytes(data)
    else:
        storage().put_bytes(f"sim/{name}", data, content_type)


def _get(name: str) -> bytes | None:
    if _local():
        path = RUNS / name
        return path.read_bytes() if path.exists() else None
    try:
        return storage().get_bytes(f"sim/{name}")
    except Exception:  # noqa: BLE001 - missing object
        return None


def _voices() -> dict:
    return json.loads(VOICES.read_text(encoding="utf-8")) if VOICES.exists() else {}


def interviewer_voice(twin_id: str) -> str | None:
    """Simulated twins get an interviewer voice that's easy to tell apart from the persona."""
    if _local():
        return _voices().get(str(twin_id))
    raw = _get(f"voices/{twin_id}.txt")
    return raw.decode() if raw else None


def persona(pid: str) -> dict:
    path = PERSONAS / f"{Path(pid).name}.yaml"
    if not path.exists():
        raise HTTPException(404, f"No persona '{pid}'.")
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def persona_instructions(p: dict) -> str:
    """The persona's standing instructions; mood cues arrive later from the director."""
    return (f"{p['persona'].strip()}\n\n## How to behave\n{p['how_to_be_interviewed'].strip()}\n\n"
            f"## Mood\nYour mood changes during the call; follow the newest mood note you get. "
            f"Start like this: {start_cue(p)}")


def start_cue(p: dict) -> str:
    if p.get("reactive"):
        return mood_cue(p, mood_for(p["reactive"]["start"], False))
    return p["phases"][0]["cue"]


# ---- reactive mood: the interviewer earns (or loses) the persona's energy -------------------
# Energy 0-10 → mood. Past the persona's stamina, every turn costs a little energy, and low
# energy then reads as tired rather than bored: only good turns hold it up.
FATIGUE = 0.5


def mood_for(energy: float, tired: bool) -> str:
    if energy <= 3:
        return "exhausted" if tired else "bored"
    return "excited" if energy >= 7 else "smiling"


def mood_cue(p: dict, mood: str) -> str:
    cues = {ph["mood"]: ph["cue"] for ph in p["phases"]}
    return (p.get("reactive", {}).get("cues", {}).get(mood) or cues.get(mood)
            or f"Mood now: {mood}.")


class Reaction(BaseModel):
    effect: int = Field(ge=-2, le=2)
    reason: str


def react_step(p: dict, energy: float, elapsed_s: float, effect: int) -> dict:
    tired = elapsed_s >= p["reactive"].get("stamina_s", 10**9)
    fatigue = FATIGUE if tired else 0.0
    new = max(0.0, min(10.0, energy + effect - fatigue))
    return {"energy": new, "fatigue": fatigue, "mood": mood_for(new, tired)}


class ReactIn(BaseModel):
    persona: str
    energy: float
    elapsed_s: float
    exchange: list[dict] = Field(max_length=12)  # recent lines, {"who", "text"}, oldest first


@router.post("/persona/react")
def persona_react(body: ReactIn) -> dict:
    """After each interviewer turn: how did it land with this person? Returns their new energy
    and mood, plus the mood cue for the persona's live session."""
    from app.llm import llm, prompt

    p = persona(body.persona)
    r = p["reactive"]
    old_mood = mood_for(body.energy, body.elapsed_s >= r.get("stamina_s", 10**9))
    convo = "\n".join(f"{'INTERVIEWER' if x.get('who') == 'interviewer' else 'THEM'}: "
                      f"{x.get('text', '')}" for x in body.exchange)
    out = llm().json("sim_persona", prompt("sim_persona").format(
        persona=p["persona"].strip(), lights_up=", ".join(r.get("lights_up", [])),
        dulls=", ".join(r.get("dulls", [])), energy=round(body.energy, 1), mood=old_mood),
        convo, Reaction)
    step = react_step(p, body.energy, body.elapsed_s, out.effect)
    return {**step, "effect": out.effect, "reason": out.reason,
            "cue": mood_cue(p, step["mood"]) if step["mood"] != old_mood else None}


@router.get("/personas")
def list_personas() -> list[dict]:
    return [{k: persona(f.stem).get(k) for k in PUBLIC_KEYS} for f in sorted(PERSONAS.glob("*.yaml"))]


def sim_owner() -> str:
    """In the open demo, simulated twins live in the shared space (labelled "simulated") so
    anyone can look through their interviews; otherwise they get their own creator."""
    return SHARED_CREATOR if get_settings().open_access else SIM_CREATOR


class SetupIn(BaseModel):
    persona: str


@router.post("/sim/setup")
def setup(body: SetupIn) -> dict:
    """Creator side, as a person would do it: brief → spec (real compiler) → approve → invite.
    Session length comes from the persona card so the wrap-up logic is exercised."""
    p = persona(body.persona)
    uid = sim_owner()
    with db.tx() as conn:
        conn.execute("insert into creators (id) values (%s) on conflict (id) do nothing", (uid,))
    made = creator.create_twin(creator.BriefIn(brief=p["brief"]), uid=uid)
    with db.tx() as conn:
        conn.execute("update twins set simulated = true where id = %s", (made["twin_id"],))
    spec = made["spec"]
    spec["session_prefs"].update(target_minutes=p["target_minutes"], max_minutes=p["max_minutes"])
    creator.put_spec(made["twin_id"], creator.SpecIn(spec=spec), uid=uid)
    creator.approve_spec(made["twin_id"], uid=uid)
    inv = creator.create_invite(made["twin_id"], creator.InviteIn(label=f"sim:{p['id']}"), uid=uid)
    if p.get("interviewer_voice"):
        if _local():
            voices = _voices()
            voices[made["twin_id"]] = p["interviewer_voice"]
            RUNS.mkdir(parents=True, exist_ok=True)
            VOICES.write_text(json.dumps(voices), encoding="utf-8")
        else:
            _put(f"voices/{made['twin_id']}.txt", p["interviewer_voice"].encode(), "text/plain")
    return {"twin_id": made["twin_id"], "token": inv["link"].rsplit("/", 1)[1],
            "persona": {k: p.get(k) for k in PUBLIC_KEYS}}


class PersonaConnectIn(BaseModel):
    persona: str
    sdp: str = Field(max_length=100_000)


@router.post("/persona/connect")
def persona_connect(body: PersonaConnectIn) -> dict:
    p = persona(body.persona)
    return open_live(persona_instructions(p), body.sdp, p.get("voice") or "marin")


class RunIn(BaseModel):
    session_id: str
    twin_id: str
    persona: str
    t0_epoch_ms: float
    phases: list[dict]
    persona_said: list[dict]
    persona_heard: list[dict]
    notes: list[str] = Field(default_factory=list)
    energy: list[dict] = Field(default_factory=list)  # reactive runs: one entry per judged turn


@router.get("/sim/twins/{twin_id}/latest-session")
def latest_session(twin_id: str) -> dict:
    with db.tx() as conn:
        row = db.one(conn, """select s.id::text from sessions s join twins t on t.id = s.twin_id
            where s.twin_id = %s and t.simulated order by s.started_at desc limit 1""",
                     (twin_id,))
    if not row:
        raise HTTPException(404, "No session for this twin yet.")
    return {"session_id": row["id"]}


@router.post("/sim/runs")
def save_run(body: RunIn) -> dict:
    _put(f"{body.session_id}.json",
         json.dumps(body.model_dump(), indent=2, ensure_ascii=False).encode(), "application/json")
    with db.tx() as conn:
        conn.execute("update sessions set stats = coalesce(stats,'{}'::jsonb) || %s where id=%s",
                     (db.j({"sim_persona": body.persona, "rec_layout": "interviewer_left"}),
                      body.session_id))
    return {"saved": True}


def _run_path(session_id: str, ext: str) -> Path:
    return RUNS / f"{Path(session_id).name}.{ext}"


@router.post("/sim/runs/{session_id}/recording-upload")
def recording_upload(session_id: str) -> dict:
    """Where the browser uploads the stereo WebM (interviewer left, persona right): straight
    to storage when deployed, since the file is larger than a function request may be."""
    sid = Path(session_id).name
    if _local():
        return {"url": f"/api/dev/sim/runs/{sid}/recording", "method": "PUT", "headers": {}}
    return storage().signed_upload(f"sim/{sid}.webm", "audio/webm")


@router.put("/sim/runs/{session_id}/recording")
async def save_recording(session_id: str, request: Request) -> dict:
    RUNS.mkdir(parents=True, exist_ok=True)
    _run_path(session_id, "webm").write_bytes(await request.body())
    return {"saved": True}


@router.get("/sim/runs")
def list_runs() -> list[dict]:
    if _local():
        out = []
        runs = [f for f in RUNS.glob("*.json") if f != VOICES]
        for f in sorted(runs, key=lambda x: x.stat().st_mtime, reverse=True):
            run = json.loads(f.read_text(encoding="utf-8"))
            out.append({"session_id": f.stem, "persona": run["persona"], "twin_id": run["twin_id"],
                        "recording": _run_path(f.stem, "webm").exists(),
                        "scorecard": _run_path(f.stem, "md").exists(),
                        "saved_at": f.stat().st_mtime})
        return out
    with db.tx() as conn:  # deployed: the session row knows it was a simulated run
        rows = db.all_(conn, """select id::text as session_id, twin_id::text, stats->>'sim_persona' persona,
            extract(epoch from started_at) saved_at from sessions where stats ? 'sim_persona'
            order by started_at desc limit 50""")
    return [{**r, "recording": True, "scorecard": True} for r in rows]


@router.get("/sim/runs/{session_id}/recording", response_model=None)
def get_recording(session_id: str) -> FileResponse | RedirectResponse:
    if not _local():
        return RedirectResponse(storage().signed_read_url(f"sim/{Path(session_id).name}.webm"))
    path = _run_path(session_id, "webm")
    if not path.exists():
        raise HTTPException(404, "No recording for this run.")
    return FileResponse(path, media_type="audio/webm")


@router.get("/sim/runs/{session_id}/scorecard")
def get_scorecard(session_id: str) -> PlainTextResponse:
    md = _get(f"{Path(session_id).name}.md")
    if md is None:
        raise HTTPException(404, "Not scored yet: uv run python scripts/sim_score.py " + session_id)
    return PlainTextResponse(md.decode("utf-8"))
