"""FastAPI app. On Vercel, api/index.py exposes it; locally, uvicorn also serves public/."""

from __future__ import annotations

import json
import logging
import time

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.cache.redis import LockBusy
from app.conversation import VoiceNotConfigured
from app.llm import LLMNotConfigured
from app.offline import pipeline  # noqa: F401  (registers job handlers)
from app.offline.deepgram import DeepgramNotConfigured
from app.routers import creator, machine, subject
from app.security.caps import CapExceeded
from app.settings import ROOT, get_settings


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        out = {"level": record.levelname, "logger": record.name, "msg": record.getMessage()}
        if record.exc_info:
            out["exc"] = self.formatException(record.exc_info)
        return json.dumps(out)


_h = logging.StreamHandler()
_h.setFormatter(JsonFormatter())
logging.basicConfig(level=logging.INFO, handlers=[_h], force=True)
log = logging.getLogger("ti.http")

app = FastAPI(title="Twin Interviewer", docs_url="/api/docs", openapi_url="/api/openapi.json")
app.include_router(creator.router)
app.include_router(subject.router)
app.include_router(machine.router)

if get_settings().dev_auth or get_settings().sim_key:
    # Simulator: persona agents take real interviews (docs/simulator.md). Off in production
    # unless SIM_KEY is set, and then every route needs that key.
    from fastapi import Depends

    from app.routers import dev

    app.include_router(dev.router)

    @app.get("/dev/sim", include_in_schema=False, dependencies=[Depends(dev.require_key)])
    def _sim():
        return FileResponse(ROOT / "devtools" / "sim.html")

    @app.get("/dev/sim.js", include_in_schema=False)
    def _sim_js():
        return FileResponse(ROOT / "devtools" / "sim.js", media_type="text/javascript")


@app.middleware("http")
async def access_log(request: Request, call_next):
    t0 = time.perf_counter()
    response = await call_next(request)
    if request.url.path.startswith("/api/"):
        log.info(json.dumps({"path": request.url.path, "method": request.method,
                             "status": response.status_code,
                             "ms": int((time.perf_counter() - t0) * 1000)}))
    elif "cache-control" not in response.headers:
        # Pages and scripts: always revalidate (cheap 304s). Without this the browser may keep
        # running an old voice.js after a change, heuristically cached from Last-Modified.
        response.headers["cache-control"] = "no-cache"
    return response


def _err(status: int, msg: str) -> JSONResponse:
    return JSONResponse({"detail": msg}, status_code=status)


@app.exception_handler(CapExceeded)
def _cap(_, exc: CapExceeded):
    return _err(429, str(exc))


@app.exception_handler(LockBusy)
def _busy(_, exc: LockBusy):
    return _err(409, "Still working on the previous message. Retry in a moment.")


@app.exception_handler(LLMNotConfigured)
@app.exception_handler(VoiceNotConfigured)
@app.exception_handler(DeepgramNotConfigured)
def _unconfigured(_, exc: Exception):
    return _err(503, str(exc))


# ---- local static serving (Vercel serves public/ and rewrites itself) -----------------------
PUBLIC = ROOT / "public"


@app.get("/s/{token}", include_in_schema=False)
def _subject(token: str):
    return FileResponse(PUBLIC / "subject.html")


@app.get("/s/{token}/voice", include_in_schema=False)
def _voice(token: str):
    return FileResponse(PUBLIC / "voice.html")


@app.get("/s/{token}/chat", include_in_schema=False)
def _chat(token: str):
    return FileResponse(PUBLIC / "chat.html")


@app.get("/creator", include_in_schema=False)
def _creator():
    return FileResponse(PUBLIC / "creator.html")


@app.get("/design-doc", include_in_schema=False)
def _design_doc():   # the AI design, short; the whole system is at /design-doc/full
    return FileResponse(PUBLIC / "design-doc.html")


@app.get("/design-doc-v2", include_in_schema=False)
def _design_doc_v2():   # the AI design plus a proposed video feed for reading emotion
    return FileResponse(PUBLIC / "design-doc-v2.html")


@app.get("/design-doc/full", include_in_schema=False)
def _design_doc_full():
    return FileResponse(PUBLIC / "design-doc-full.html")


if PUBLIC.exists():
    app.mount("/", StaticFiles(directory=PUBLIC, html=True), name="public")
