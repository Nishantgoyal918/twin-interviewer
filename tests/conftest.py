"""Test setup: embedded Postgres (pgserver, with pgvector), in-memory cache, inline jobs,
local storage, fake LLM. No network, no keys."""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.update({
    "CACHE_BACKEND": "memory", "QUEUE_MODE": "inline", "STORAGE_BACKEND": "local",
    "LLM_BACKEND": "fake", "DEV_AUTH": "1", "OPENAI_API_KEY": "", "DEEPGRAM_API_KEY": "",
    "APP_BASE_URL": "http://testserver", "INVITE_TOKEN_SECRET": "test-secret",
    "DEEPGRAM_WEBHOOK_SECRET": "dg-secret", "CRON_SECRET": "cron-secret",
})

logging.getLogger("pgserver").disabled = True

TABLES = ("voice_examples, labels, eval_runs, jobs, audio_assets, do_not_raise, interests, "
          "register_coverage, coverage_slots, planner_snapshots, notes, baselines, signals, "
          "observer_outputs, corpus_items, imports, turns, sessions, subject_invites, "
          "twin_specs, twins, creators")


@pytest.fixture(scope="session")
def database_url() -> str:
    import pgserver
    import psycopg

    data = ROOT / ".data" / "pg-test"
    data.mkdir(parents=True, exist_ok=True)
    srv = pgserver.get_server(str(data), cleanup_mode=None)
    base = srv.get_uri()
    with psycopg.connect(base, autocommit=True) as conn:
        conn.execute("drop database if exists ti_test")
        conn.execute("create database ti_test")
    url = base.rsplit("/", 1)[0] + "/ti_test"
    os.environ["DATABASE_URL"] = url
    from app.settings import get_settings

    get_settings.cache_clear()
    from scripts.migrate import migrate

    migrate(url)
    return url


@pytest.fixture(autouse=True)
def clean(request):
    """Fresh fakes per test; DB truncated for tests that use it."""
    from app import llm
    from app.cache import redis as cache
    from app.services import context

    cache.set_backend(cache.MemoryBackend())
    llm.set_llm(llm.FakeLLM())
    context._spec_memo.clear()
    if "database_url" in request.fixturenames or "client" in request.fixturenames:
        request.getfixturevalue("database_url")
        from app import db

        with db.tx() as conn:
            conn.execute(f"truncate {TABLES} cascade")
    yield


@pytest.fixture
def client(database_url):
    from fastapi.testclient import TestClient

    from app.main import app

    return TestClient(app)


@pytest.fixture
def fake_llm():
    from app.llm import llm

    return llm()


CREATOR = {"x-dev-creator": "11111111-1111-1111-1111-111111111111"}


@pytest.fixture
def twin(client):
    """An approved twin with an invite token."""
    r = client.post("/api/twins", json={"brief": "A twin of my mother so I can ask her recipes."},
                    headers=CREATOR)
    assert r.status_code == 200, r.text
    twin_id = r.json()["twin_id"]
    assert client.post(f"/api/twins/{twin_id}/spec/approve", headers=CREATOR).status_code == 200
    inv = client.post(f"/api/twins/{twin_id}/invites", json={}, headers=CREATOR).json()
    return {"id": twin_id, "token": inv["link"].rsplit("/", 1)[1]}
