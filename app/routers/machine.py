"""Machine endpoints: QStash jobs, Deepgram webhook, daily cron, health, local dev storage."""

from __future__ import annotations

import hmac
import time

from fastapi import APIRouter, HTTPException, Request, Response

from app import db, queue
from app.offline import pipeline
from app.services.sessions import sweep_idle
from app.settings import get_settings
from app.storage import LocalStorage, storage

router = APIRouter(prefix="/api")


@router.get("/health")
def health() -> dict:
    s = get_settings()
    with db.tx() as conn:
        conn.execute("select 1")
    return {"ok": True, "cache": s.cache_backend, "queue": s.queue_mode,
            "storage": s.storage_backend, "llm": s.llm_backend,
            "voice_ready": bool(s.openai_api_key), "deepgram_ready": bool(s.deepgram_api_key)}


@router.post("/jobs/{kind}")
async def run_job(kind: str, request: Request) -> dict:
    body = await request.body()
    url = f"{get_settings().app_base_url}/api/jobs/{kind}"
    if not queue.verify_qstash(request.headers.get("upstash-signature"), body, url):
        raise HTTPException(401, "bad signature")
    job_id = (await request.json()).get("job_id")
    return {"result": queue.run(job_id)}


@router.post("/webhooks/deepgram")
async def deepgram_webhook(request: Request, asset: str, secret: str) -> dict:
    if not hmac.compare_digest(secret, get_settings().deepgram_webhook_secret):
        raise HTTPException(401, "bad secret")
    pipeline.deepgram_callback(asset, await request.json())
    return {"ok": True}


@router.get("/cron/daily")
def cron_daily(request: Request) -> dict:
    expected = f"Bearer {get_settings().cron_secret}"
    if not hmac.compare_digest(request.headers.get("authorization", ""), expected):
        raise HTTPException(401, "bad cron secret")
    with db.tx() as conn:
        conn.execute("select 1")  # keeps Supabase from pausing
        stuck = db.all_(conn, """select id, kind from jobs where
             (status in ('queued','running') and updated_at < now() - interval '30 minutes'
              and attempts < 5)
          or (status = 'failed' and attempts < 3)""")
        unended = db.all_(conn, """select s.id from sessions s where s.status='ended'
             and not exists (select 1 from jobs j where j.ref_id=s.id and j.kind='analyze_backlog')""")
    ended = sweep_idle()
    for j in stuck:
        queue.dispatch(j["kind"], str(j["id"]))
    for s in unended:
        queue.enqueue("analyze_backlog", str(s["id"]), idem=f"backlog:{s['id']}")
    return {"ended_sessions": len(ended), "redispatched_jobs": len(stuck),
            "backlog_enqueued": len(unended)}


def _local(path: str, method: str, exp: int, sig: str) -> LocalStorage:
    st = storage()
    if not isinstance(st, LocalStorage):
        raise HTTPException(404, "not found")
    if exp < time.time() or not hmac.compare_digest(sig, LocalStorage.sign(path, method, exp)):
        raise HTTPException(403, "bad or expired link")
    return st


@router.put("/dev/storage/{path:path}")
async def dev_put(path: str, exp: int, sig: str, request: Request) -> dict:
    st = _local(path, "PUT", exp, sig)
    st.put_bytes(path, await request.body(), request.headers.get("content-type", ""))
    return {"ok": True}


@router.get("/dev/storage/{path:path}")
def dev_get(path: str, exp: int, sig: str) -> Response:
    st = _local(path, "GET", exp, sig)
    return Response(st.get_bytes(path), media_type="application/octet-stream")
