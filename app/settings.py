"""Env-driven settings + config.yaml. Loaded once per process."""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import yaml

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv() -> None:
    """Minimal .env loader for local dev; real env vars win."""
    path = ROOT / ".env"
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


@dataclass(frozen=True)
class Settings:
    openai_api_key: str
    deepgram_api_key: str
    supabase_url: str
    supabase_anon_key: str
    supabase_service_role_key: str
    database_url: str
    upstash_redis_rest_url: str
    upstash_redis_rest_token: str
    qstash_token: str
    qstash_current_signing_key: str
    qstash_next_signing_key: str
    invite_token_secret: str
    deepgram_webhook_secret: str
    cron_secret: str
    app_base_url: str
    cache_backend: str
    queue_mode: str
    storage_backend: str
    llm_backend: str
    dev_auth: bool
    sim_key: str  # set: the simulator runs here too (needs ?key=), e.g. on the deployed app
    open_access: bool  # public demo: no sign-in, everyone shares one creator and its twins
    config: dict[str, Any]

    def cfg(self, dotted: str, default: Any = None) -> Any:
        node: Any = self.config
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node


def _clean_db_url(url: str) -> str:
    """Supabase's Vercel integration adds `supa=...` to its pooled URL; libpq rejects unknown
    query parameters, so drop it."""
    parts = urlsplit(url)
    query = [(k, v) for k, v in parse_qsl(parts.query) if k != "supa"]
    return urlunsplit(parts._replace(query=urlencode(query))) if parts.query else url


@lru_cache
def get_settings() -> Settings:
    _load_dotenv()
    env = os.environ.get
    config = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    return Settings(
        openai_api_key=env("OPENAI_API_KEY", ""),
        deepgram_api_key=env("DEEPGRAM_API_KEY", ""),
        supabase_url=env("SUPABASE_URL", "").rstrip("/"),
        supabase_anon_key=env("SUPABASE_ANON_KEY", ""),
        supabase_service_role_key=env("SUPABASE_SERVICE_ROLE_KEY", ""),
        # Vercel Marketplace integrations (Supabase, Upstash) set their own names; accept both.
        database_url=_clean_db_url(env("DATABASE_URL") or env("POSTGRES_URL", "")),
        upstash_redis_rest_url=env("UPSTASH_REDIS_REST_URL") or env("KV_REST_API_URL", ""),
        upstash_redis_rest_token=env("UPSTASH_REDIS_REST_TOKEN") or env("KV_REST_API_TOKEN", ""),
        qstash_token=env("QSTASH_TOKEN", ""),
        qstash_current_signing_key=env("QSTASH_CURRENT_SIGNING_KEY", ""),
        qstash_next_signing_key=env("QSTASH_NEXT_SIGNING_KEY", ""),
        invite_token_secret=env("INVITE_TOKEN_SECRET", "dev-secret"),
        deepgram_webhook_secret=env("DEEPGRAM_WEBHOOK_SECRET", "dev-secret"),
        cron_secret=env("CRON_SECRET", "dev-secret"),
        app_base_url=env("APP_BASE_URL", "http://localhost:8000").rstrip("/"),
        cache_backend=env("CACHE_BACKEND", "upstash"),
        queue_mode=env("QUEUE_MODE", "qstash"),
        storage_backend=env("STORAGE_BACKEND", "supabase"),
        llm_backend=env("LLM_BACKEND", "openai"),
        dev_auth=env("DEV_AUTH", "") == "1",
        open_access=env("OPEN_ACCESS", "") == "1",
        sim_key=env("SIM_KEY", ""),
        config=config,
    )
