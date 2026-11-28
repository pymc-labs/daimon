"""SchedulerSettings — adapter-local config (env prefix DAIMON_SCHEDULER__).

Kept on the adapter so core ``Settings`` stays clean of adapter-specific
fields (matches ``DiscordSettings``/``McpSettings`` boundary).
"""

from __future__ import annotations

from daimon.core.dispatch_timeout import DispatchTimeoutSettings
from pydantic import Field
from pydantic_settings import SettingsConfigDict


class SchedulerSettings(DispatchTimeoutSettings):
    model_config = SettingsConfigDict(
        env_prefix="DAIMON_SCHEDULER__",
        env_nested_delimiter="__",
        extra="ignore",
    )

    tick_interval_s: float = Field(
        default=30.0,
        description=(
            "Seconds between scheduler ticks (loop sleep). The usage sweep "
            "runs on its own loop and pauses the same interval between passes."
        ),
    )

    usage_sweep_enabled: bool = Field(
        default=True,
        description=(
            "Run the headless usage sweep: settle the MCP agent-chat sessions "
            "whose turns nobody read to the end (each headless send leaves a "
            "settle request; a status read that sees the turn end settles it "
            "first). It reads only the sessions those requests name. When off, "
            "such a session is billed the next time a read sees its last turn "
            "ended."
        ),
    )

    max_concurrent_fires: int = Field(
        default=10,
        description=(
            "Global cap on simultaneously-dispatched routine fires across all "
            "ticks. Conservative against the shared Anthropic key's rate "
            "limit; per-tenant caps are enforced separately by the adapters."
        ),
    )

    advisory_lock_key: int = Field(
        default=0x44_41_49_4D_4F_4E_53_43,
        description=(
            "Postgres pg_try_advisory_lock int64 key. Default is the ASCII "
            "encoding of 'DAIMONSC'. Two scheduler processes share the key; "
            "the second logs that it did not get the lock and exits non-zero."
        ),
    )

    health_port: int = Field(
        default=8082,
        description=(
            "Port for the stdlib liveness responder (used by the platform "
            "health check). "
            "Must not collide with mcp's 8080 or discord's 8081 on the "
            "shared host."
        ),
    )
