"""Process-local, windowed health logging for long-running adapters."""

from __future__ import annotations

import asyncio
import logging
import math
import uuid
from collections import Counter
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager, suppress
from typing import TypedDict, cast

import httpx
import structlog
from daimon.core.session_preparation_gate import preparation_counts
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlalchemy.pool import QueuePool

_responses: Counter[tuple[str, str]] = Counter()
_remaining: dict[str, int] = {}
_turns_by_tenant: Counter[uuid.UUID] = Counter()
_turns_global = 0


class DiscordRateLimitCount(TypedDict):
    count: int
    max_retry_s: float


_discord_ratelimits: dict[str, DiscordRateLimitCount] = {}


def _discord_route(method: str, url: str) -> str:
    path = url.split("?", 1)[0].split("/api/", 1)[-1]
    if method == "POST" and "/messages/" in path and path.endswith("/threads"):
        return "thread_create"
    if method == "POST" and path.endswith("/messages"):
        return "message_send"
    if method == "PATCH" and "/messages/" in path:
        return "message_edit"
    return "other"


class DiscordRateLimitFilter(logging.Filter):
    """Observe discord.py's retry warning without changing its log delivery."""

    def filter(self, record: logging.LogRecord) -> bool:
        if (
            record.name == "discord.http"
            and isinstance(record.msg, str)
            and record.msg.startswith("We are being rate limited.")
            and "Retrying in" in record.msg
            and isinstance(record.args, tuple)
            and len(record.args) >= 3
        ):
            method, url, delay = record.args[:3]
            if isinstance(method, str) and isinstance(url, str) and isinstance(delay, (int, float)):
                route = _discord_route(method, url)
                entry = _discord_ratelimits.setdefault(route, {"count": 0, "max_retry_s": 0.0})
                entry["count"] += 1
                entry["max_retry_s"] = max(entry["max_retry_s"], delay)
        return True


def take_discord_ratelimit_window() -> dict[str, DiscordRateLimitCount]:
    counts = dict(_discord_ratelimits)
    _discord_ratelimits.clear()
    return counts


@contextmanager
def track_turn(tenant_id: uuid.UUID | None) -> Iterator[None]:
    """Track one active observation; callers avoid nesting the same observation."""
    global _turns_global
    _turns_global += 1
    if tenant_id is not None:
        _turns_by_tenant[tenant_id] += 1
    try:
        yield
    finally:
        _turns_global -= 1
        if tenant_id is not None:
            _turns_by_tenant[tenant_id] -= 1
            if not _turns_by_tenant[tenant_id]:
                del _turns_by_tenant[tenant_id]


def current_turn_counts() -> tuple[int, int | None]:
    return _turns_global, max(_turns_by_tenant.values(), default=0)


def _endpoint(path: str) -> str:
    parts = path.strip("/").split("/")
    if parts and parts[0].startswith("v"):
        parts = parts[1:]
    if "messages" in parts:
        return "messages"
    if any(part in {"sessions", "agents", "environments"} for part in parts):
        return "sessions_agents_environments"
    if "skills" in parts:
        return "skills"
    if "files" in parts:
        return "files"
    return "other"


def _status(code: int) -> str:
    if code == 429:
        return "429"
    if code == 529:
        return "529"
    if 200 <= code < 300:
        return "2xx"
    if 400 <= code < 500:
        return "other_4xx"
    if 500 <= code < 600:
        return "5xx"
    return "other"


def record_anthropic_response(request: httpx.Request, response: httpx.Response) -> None:
    """Count one transport response, including responses retried by the SDK."""
    _responses[(_endpoint(request.url.path), _status(response.status_code))] += 1
    for name, value in response.headers.items():
        if name.startswith("anthropic-ratelimit-") and name.endswith("-remaining"):
            try:
                number = int(value)
            except ValueError:
                continue
            _remaining[name] = min(number, _remaining.get(name, number))


def take_anthropic_window() -> tuple[dict[str, dict[str, int]], dict[str, int]]:
    """Return and clear counts and minimum remaining quotas; no await, so atomic on the loop."""
    counts: dict[str, dict[str, int]] = {}
    for (endpoint, status), count in _responses.items():
        counts.setdefault(endpoint, {})[status] = count
    remaining = dict(_remaining)
    _responses.clear()
    _remaining.clear()
    return counts, remaining


def _percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[math.ceil(len(ordered) * fraction) - 1]


async def log_health_once(
    process: str,
    engine: AsyncEngine,
    lag_samples_ms: list[float],
    turns: Callable[[], tuple[int, int | None]],
    interval_s: float = 30,
) -> None:
    counts, remaining = take_anthropic_window()
    global_turns, per_tenant_max = turns()
    pool = cast(QueuePool, engine.sync_engine.pool)
    fields: dict[str, object] = dict(
        process=process,
        interval_s=interval_s,
        anthropic_responses=counts,
        anthropic_ratelimit_remaining_min=remaining,
        db_pool={"checkedout": pool.checkedout(), "overflow": pool.overflow(), "size": pool.size()},
        prep_gate=preparation_counts(),
        loop_lag_ms={
            "max": max(lag_samples_ms, default=0.0),
            "p95": _percentile(lag_samples_ms, 0.95),
        },
        turns_in_flight={"global": global_turns, "per_tenant_max": per_tenant_max},
    )
    if process == "discord":
        fields["discord_ratelimits"] = take_discord_ratelimit_window()
    structlog.get_logger().info("runtime.health", **fields)


async def _run_health(
    process: str,
    engine: AsyncEngine,
    interval_s: float,
    turns: Callable[[], tuple[int, int | None]],
) -> None:
    loop = asyncio.get_running_loop()
    samples: list[float] = []
    window_start = loop.time()
    next_probe = loop.time() + 1.0
    next_log = window_start + interval_s
    while True:
        await asyncio.sleep(max(0.0, min(next_probe, next_log) - loop.time()))
        now = loop.time()
        if now >= next_probe:
            samples.append(max(0.0, (now - next_probe) * 1000))
            next_probe = now + 1.0
        if now >= next_log:
            await log_health_once(process, engine, samples, turns, interval_s)
            samples = []
            window_start = now
            next_log = window_start + interval_s


@asynccontextmanager
async def runtime_health(
    process: str,
    engine: AsyncEngine,
    interval_s: float,
    turns: Callable[[], tuple[int, int | None]] | None = None,
) -> AsyncIterator[None]:
    """Start the process health heartbeat; interval zero disables it."""
    discord_logger = logging.getLogger("discord.http") if process == "discord" else None
    rate_limit_filter = DiscordRateLimitFilter() if discord_logger is not None else None
    if discord_logger is not None and rate_limit_filter is not None:
        discord_logger.addFilter(rate_limit_filter)
    task = (
        asyncio.create_task(_run_health(process, engine, interval_s, turns or (lambda: (0, None))))
        if interval_s > 0
        else None
    )
    try:
        yield
    finally:
        if discord_logger is not None and rate_limit_filter is not None:
            discord_logger.removeFilter(rate_limit_filter)
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
