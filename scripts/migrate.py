"""Applies migrations/*.sql in order, once each. Usage: uv run python scripts/migrate.py"""

from __future__ import annotations

import sys
from pathlib import Path

import psycopg

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.settings import get_settings  # noqa: E402


def migrate(database_url: str | None = None) -> list[str]:
    url = database_url or get_settings().database_url
    applied = []
    with psycopg.connect(url, autocommit=True, prepare_threshold=None) as conn:
        conn.execute("create table if not exists schema_migrations (name text primary key, "
                     "applied_at timestamptz default now())")
        done = {r[0] for r in conn.execute("select name from schema_migrations").fetchall()}
        for path in sorted((ROOT / "migrations").glob("*.sql")):
            if path.name in done:
                continue
            with conn.transaction():
                conn.execute(path.read_text(encoding="utf-8"))
                conn.execute("insert into schema_migrations (name) values (%s)", (path.name,))
            applied.append(path.name)
    return applied


if __name__ == "__main__":
    names = migrate()
    print("applied:", ", ".join(names) if names else "nothing new")
