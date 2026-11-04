"""Postgres access. Postgres is the source of truth (plan 6.1)."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from psycopg import Connection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from app.settings import get_settings

_pool: ConnectionPool | None = None


def pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        # prepare_threshold=None: the Supabase transaction pooler can't hold prepared statements.
        _pool = ConnectionPool(
            get_settings().database_url,
            min_size=0,
            max_size=4,
            kwargs={"prepare_threshold": None, "row_factory": dict_row, "autocommit": False},
            open=True,
        )
    return _pool


def reset_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
    _pool = None


@contextmanager
def tx() -> Iterator[Connection]:
    """One transaction. Commits on success, rolls back on error."""
    with pool().connection() as conn:
        with conn.transaction():
            yield conn


def one(conn: Connection, sql: str, params: Any = None) -> dict | None:
    return conn.execute(sql, params).fetchone()


def all_(conn: Connection, sql: str, params: Any = None) -> list[dict]:
    return conn.execute(sql, params).fetchall()


def j(value: Any) -> Jsonb:
    return Jsonb(value)
