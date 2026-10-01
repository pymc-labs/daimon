"""Process health windows count transport attempts and emit bounded snapshots."""

import asyncio
import uuid
from unittest.mock import Mock

import httpx
import pytest
from anthropic import AsyncAnthropic, DefaultAsyncHttpxClient
from daimon.core.config import load_settings
from daimon.core.runtime_health import (
    current_turn_counts,
    log_health_once,
    runtime_health,
    take_anthropic_window,
    track_turn,
)
from daimon.core.skills.rate_limit import SkillsRateLimitedTransport
from sqlalchemy.ext.asyncio import create_async_engine


async def test_sdk_retry_counts_each_attempt_and_window_resets() -> None:
    await take_anthropic_window()
    attempts = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(
                429,
                json={"type": "error", "error": {"type": "rate_limit_error"}},
                headers={"retry-after": "0", "anthropic-ratelimit-requests-remaining": "3"},
                request=request,
            )
        return httpx.Response(
            200,
            json={"data": [], "has_more": False},
            headers={"anthropic-ratelimit-requests-remaining": "5"},
            request=request,
        )

    transport = SkillsRateLimitedTransport(80, inner=httpx.MockTransport(respond))
    async with AsyncAnthropic(
        api_key="test", max_retries=1, http_client=DefaultAsyncHttpxClient(transport=transport)
    ) as client:
        await client.models.list()
    counts, remaining = await take_anthropic_window()
    assert attempts == 2
    assert counts == {"other": {"429": 1, "2xx": 1}}
    assert remaining == {"anthropic-ratelimit-requests-remaining": 3}
    assert await take_anthropic_window() == ({}, {})


async def test_health_interval_and_emitted_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    logger = Mock()
    monkeypatch.setattr("daimon.core.runtime_health.structlog.get_logger", lambda: logger)
    engine = create_async_engine("postgresql+asyncpg://test:test@localhost/test")
    try:
        async with runtime_health("test", engine, 0):
            await asyncio.sleep(0.03)
        assert logger.info.call_count == 0
        async with runtime_health("test", engine, 0.02, lambda: (2, 1)):
            await asyncio.sleep(0.06)
        assert logger.info.call_count >= 1
        (event,) = logger.info.call_args.args
        fields = logger.info.call_args.kwargs
        assert event == "runtime.health"
        assert set(fields) == {
            "process",
            "interval_s",
            "anthropic_responses",
            "anthropic_ratelimit_remaining_min",
            "db_pool",
            "loop_lag_ms",
            "turns_in_flight",
        }
        assert fields["turns_in_flight"] == {"global": 2, "per_tenant_max": 1}
        assert set(fields["db_pool"]) == {"checkedout", "overflow", "size"}
        assert set(fields["loop_lag_ms"]) == {"max", "p95"}
        await log_health_once("test", engine, [4.0, 8.0], lambda: (0, None))
        assert logger.info.call_args.kwargs["loop_lag_ms"] == {"max": 8.0, "p95": 8.0}
    finally:
        await engine.dispose()


def test_health_interval_setting_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DAIMON_DATABASE__URL", "postgresql+asyncpg://test:test@localhost/test")
    monkeypatch.setenv("DAIMON_ANTHROPIC__API_KEY", "test")
    monkeypatch.setenv("DAIMON_OBSERVABILITY__HEALTH_INTERVAL_S", "0")
    assert load_settings(_env_file=None).observability.health_interval_s == 0


def test_turn_counts_are_process_local_and_released() -> None:
    tenant = uuid.uuid4()
    assert current_turn_counts() == (0, 0)
    with track_turn(tenant):
        assert current_turn_counts() == (1, 1)
        with track_turn(tenant):
            assert current_turn_counts() == (2, 2)
    assert current_turn_counts() == (0, 0)
