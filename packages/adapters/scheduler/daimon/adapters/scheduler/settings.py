"""SchedulerSettings — adapter-local config (env prefix DAIMON_SCHEDULER__).

Kept on the adapter so core ``Settings`` stays clean of adapter-specific
fields (matches ``DiscordSettings``/``McpSettings`` boundary).
"""

from __future__ import annotations

from daimon.core.turn.ceiling import TURN_CEILING_S
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# Headroom above the core ~45-minute ceiling for the pre-turn adapter work
# `_fire` does OUTSIDE headless_runner's two ceiling-covered legs (routine
# row bookkeeping, agent/environment resolution, the usage-recorder factory,
# `record_result`). `dispatch_timeout_s`'s default is derived from this
# constant plus `TURN_CEILING_S` rather than hardcoded, so the inversion
# 19-VERIFICATION.md found (an outer guard sitting BELOW the core ceiling
# it's supposed to backstop) is structurally impossible to reintroduce.
DISPATCH_TIMEOUT_MARGIN_S: float = 300.0


class SchedulerSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="DAIMON_SCHEDULER__",
        env_nested_delimiter="__",
        extra="ignore",
    )

    tick_interval_s: float = Field(
        default=30.0,
        description="Seconds between scheduler ticks (loop sleep).",
    )

    max_age_s: float = Field(
        default=900.0,
        description=(
            "Freshness window — rows whose next_fire_at slipped past "
            "now - max_age_s are advanced via advance_stale and not fired."
        ),
    )

    max_concurrent_fires: int = Field(
        default=10,
        description=(
            "Global cap on simultaneously-dispatched routine fires within one "
            "tick. Conservative against the shared Anthropic key's rate "
            "limit; per-tenant caps are enforced separately by the adapters."
        ),
    )

    dispatch_timeout_s: float = Field(
        default=TURN_CEILING_S + DISPATCH_TIMEOUT_MARGIN_S,
        description=(
            "An OUTER PROCESS GUARD (asyncio.wait_for), not the turn's "
            "deadline. The core ~45-minute ceiling (daimon.core.turn.ceiling) "
            "is enforced inside headless_runner.run_turn and fires first, "
            "producing a TurnError(kind='ceiling'). This bound only catches a "
            "fire that hangs OUTSIDE the ceiling's two legs (routine row "
            "bookkeeping, agent/environment resolution, the usage-recorder "
            "factory, record_result), and must stay strictly above "
            "TURN_CEILING_S or the core ceiling becomes unreachable for "
            "routines. run_one_tick awaits the full gather, so a fire "
            "running to this bound blocks the tick loop for that long, and "
            "cron slots that slip more than max_age_s (default 900s) behind "
            "during that window are advanced by advance_stale rather than "
            "fired."
        ),
    )

    advisory_lock_key: int = Field(
        default=0x44_41_49_4D_4F_4E_53_43,
        description=(
            "Postgres pg_try_advisory_lock int64 key. Default is the ASCII "
            "encoding of 'DAIMONSC'. Two scheduler processes share the key; "
            "the second exits cleanly."
        ),
    )

    health_port: int = Field(
        default=8082,
        description=(
            "Port for the stdlib liveness responder (Fly health check). "
            "Must not collide with mcp's 8080 or discord's 8081 on the "
            "shared host."
        ),
    )
