"""Rate limits, daily caps and the global spend breaker (plan 10.6).

Counters live only in Redis. When Redis is unreachable, new voice sessions are refused
(fail closed); text keeps working.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime

from app.cache import redis as cache
from app.settings import get_settings


class CapExceeded(RuntimeError):
    pass


def today() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d")


def check_ip(ip: str) -> None:
    limit = int(get_settings().cfg("caps.ip_requests_per_minute", 60))
    minute = int(time.time() // 60)
    try:
        n = cache.backend().incr_by(cache.k_rl_ip(ip, minute), 1, ttl_s=120)
    except cache.CacheUnavailable:
        return
    if n > limit:
        raise CapExceeded("Too many requests. Wait a minute and try again.")


def add_spend(usd: float) -> None:
    if usd <= 0:
        return
    try:
        cache.backend().incr_by(cache.k_spend(today()), usd, ttl_s=48 * 3600)
    except cache.CacheUnavailable:
        pass


def spend_today() -> float:
    try:
        return float(cache.backend().get(cache.k_spend(today())) or 0)
    except cache.CacheUnavailable:
        return float("inf")


def check_global_spend() -> None:
    limit = float(get_settings().cfg("caps.global_spend_usd_per_day", 5.0))
    if spend_today() >= limit:
        raise CapExceeded("The app has reached today's spending limit. Try again tomorrow.")


def start_session_caps(twin_id: str, modality: str, max_minutes: int) -> int:
    """Checks caps for a new session and returns its max duration in seconds.

    Voice: min(spec max_minutes, remaining daily voice seconds for the twin). Fails closed
    if Redis is down.
    """
    cfg = get_settings().cfg
    b = cache.backend()
    day = today()
    max_s = max_minutes * 60
    if modality != "voice":
        try:
            b.incr_by(cache.k_rl_twin_sessions(twin_id, day), 1, ttl_s=48 * 3600)
        except cache.CacheUnavailable:
            pass
        return max_s
    paused = CapExceeded("Voice is paused right now (limits can't be checked).")
    if spend_today() == float("inf"):
        raise paused
    check_global_spend()
    try:
        sessions = b.incr_by(cache.k_rl_twin_sessions(twin_id, day), 1, ttl_s=48 * 3600)
        used = float(b.get(cache.k_rl_twin_seconds(twin_id, day)) or 0)
    except cache.CacheUnavailable as exc:
        raise paused from exc
    if sessions > int(cfg("caps.twin_sessions_per_day", 10)):
        raise CapExceeded("This twin has reached today's session limit.")
    remaining = int(cfg("caps.twin_voice_seconds_per_day", 1800)) - used
    if remaining < 60:
        raise CapExceeded("This twin has used today's voice minutes. Text chat still works.")
    return int(min(max_s, remaining))


def record_voice_usage(twin_id: str, seconds: float) -> None:
    cfg = get_settings().cfg
    usd = seconds / 60 * float(cfg("voice.price_per_minute_usd", 0.05))
    try:
        cache.backend().incr_by(cache.k_rl_twin_seconds(twin_id, today()), seconds, 48 * 3600)
    except cache.CacheUnavailable:
        pass
    add_spend(usd)
