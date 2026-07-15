"""SchedulerSettings — adapter-local config (env prefix DAIMON_SCHEDULER__).

Kept on the adapter so core ``Settings`` stays clean of adapter-specific
fields (matches ``DiscordSettings``/``McpSettings`` boundary).
"""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


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
        default=600.0,
        description=(
            "Per-fire wall-clock deadline (asyncio.wait_for) guarding hung "
            "connections, not legitimately long agent turns; well under "
            "max_age_s."
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
