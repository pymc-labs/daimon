"""SchedulerSettings — env-driven adapter config.

Behavior:
- Defaults are sane for local/dev (30s tick, 15min freshness window, 2min drain).
- Fields override from `DAIMON_SCHEDULER__*` env vars.
- The advisory-lock key is a stable int constant by default so two scheduler
  processes contend on the same key.
- `default_environment_id` was deleted. The scheduler now
  resolves the environment id at fire time via `daimon.core.ma_resolver`.
"""

from __future__ import annotations

import pytest
from daimon.adapters.scheduler.settings import SchedulerSettings


def test_scheduler_settings_defaults_are_sane() -> None:
    s = SchedulerSettings()
    assert s.tick_interval_s == 30.0, "default tick interval should be 30s"
    assert s.max_age_s == 900.0, "default freshness window should be 15min"
    assert isinstance(s.advisory_lock_key, int), "advisory_lock_key must be int"
    assert s.advisory_lock_key != 0, "advisory_lock_key must be a stable non-zero default"
    assert "drain_timeout_s" not in SchedulerSettings.model_fields, (
        "drain_timeout_s was removed with the _drain_running_tasks machinery"
    )


def test_scheduler_settings_reads_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DAIMON_SCHEDULER__TICK_INTERVAL_S", "5")
    s = SchedulerSettings()
    assert s.tick_interval_s == 5.0


def test_scheduler_settings_new_concurrency_defaults() -> None:
    s = SchedulerSettings()
    assert s.max_concurrent_fires == 10, "default global concurrent-fire cap should be 10"
    assert s.dispatch_timeout_s == 600.0, "default per-fire timeout should be 600s"


def test_scheduler_settings_concurrency_reads_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DAIMON_SCHEDULER__MAX_CONCURRENT_FIRES", "3")
    monkeypatch.setenv("DAIMON_SCHEDULER__DISPATCH_TIMEOUT_S", "1.5")
    s = SchedulerSettings()
    assert s.max_concurrent_fires == 3, "env should override concurrent-fire cap"
    assert s.dispatch_timeout_s == 1.5, "env should override per-fire timeout"


def test_scheduler_settings_health_port_default() -> None:
    s = SchedulerSettings()
    assert s.health_port == 8082, (
        "default health port should be 8082 (mcp owns 8080, discord 8081 — no collision)"
    )


def test_scheduler_settings_health_port_reads_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DAIMON_SCHEDULER__HEALTH_PORT", "9099")
    s = SchedulerSettings()
    assert s.health_port == 9099, "DAIMON_SCHEDULER__HEALTH_PORT must override the health port"


def test_default_environment_id_field_removed() -> None:
    """The scheduler no longer reads
    environment_id from settings — it resolves via
    `daimon.core.ma_resolver.resolve_environment` at fire time. Pin the
    deletion so a future revert doesn't silently re-add the dead config knob.
    """
    assert "default_environment_id" not in SchedulerSettings.model_fields
