"""pydantic-settings for report-host.

Constructed via `load_settings()`. Never import a module-level settings
singleton — construct once at the edge (app startup, test fixture) and
inject downstream.

Env prefix: DAIMON_REPORT__  (flat — no nested delimiter needed here, all
fields are top-level on the single Settings class).
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Annotated

from pydantic import Field, HttpUrl, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    data_dir: Path = Field(
        default=Path("/data/reports"),
        description="Root of the host's persistent volume: SQLite stores, bundles, PDFs.",
    )
    # Bearer tokens accepted on admin routes. Provide MORE than one to rotate
    # without downtime: add the new one, deploy bot with new value, drop the
    # old. `DAIMON_REPORT__ADMIN_SECRETS=primary,backup` is the canonical
    # form. Unlike the notebook host, this service has no existing deployment
    # to preserve compatibility with, so there is no singular `admin_secret`
    # alias here — a required list with a clear error is better than a
    # deprecated alias that never had users.
    admin_secrets: Annotated[list[SecretStr], NoDecode] = Field(
        default_factory=list[SecretStr],
        description=(
            "CSV of bearer tokens accepted on admin routes "
            "(DAIMON_REPORT__ADMIN_SECRETS=primary,backup). At least one is required — "
            "the host refuses to start without one."
        ),
    )

    @field_validator("admin_secrets", mode="before")
    @classmethod
    def _split_admin_secrets(cls, v: object) -> object:
        # pydantic-settings reads env as a string; comma-split into a list.
        if isinstance(v, str):
            return [s.strip() for s in v.split(",") if s.strip()]
        return v

    @model_validator(mode="after")
    def _require_admin_secret(self) -> Settings:
        if not self.admin_secrets:
            raise ValueError(
                "at least one admin bearer required: set DAIMON_REPORT__ADMIN_SECRETS (CSV)"
            )
        return self

    mcp_url: HttpUrl = Field(
        description="The seam endpoint this host calls to run turns, poll cost, and mint tokens."
    )
    public_url_base: HttpUrl = Field(
        description=(
            "External URL prefix used to build recipient links and the per-turn upload "
            "URL handed to the agent."
        )
    )
    host_port: int = Field(default=8002, description="Port the host's uvicorn server binds.")
    reserve_usd: Decimal = Field(
        default=Decimal("0.60"),
        description=(
            "Amount held against a report's spend cap the moment a turn starts, released "
            "and replaced by the real cost once the turn settles. Measured reader turns ran "
            "roughly sixteen to sixty-three cents; a revision-with-rebuild came in under "
            "this reserve."
        ),
    )
    poll_interval_seconds: float = Field(
        default=2.0, description="How often the host polls the seam for turn progress."
    )
    turn_timeout_seconds: int = Field(
        default=1200,
        description="A turn still running past this many seconds is cancelled by the host.",
    )
    max_pdf_bytes: int = Field(
        default=50 * 1024 * 1024,
        description="Hard ceiling on an uploaded revised-PDF body size.",
    )
    max_bundle_bytes: int = Field(
        default=25 * 1024 * 1024,
        description=(
            "Hard ceiling on a published report bundle. Mirrors the seam's own bundle cap "
            "and is enforced independently here as a second layer of defense."
        ),
    )
    max_open_threads_per_recipient: int = Field(
        default=3, description="Cap on concurrently open threads a single recipient may hold."
    )
    max_running_turns_per_report: int = Field(
        default=4, description="Cap on concurrently running turns across one report's threads."
    )
    recipient_link_ttl_days: int = Field(
        default=90, description="How long a per-recipient link remains valid before expiring."
    )
    thread_idle_archive_hours: int = Field(
        default=24,
        description="A thread idle longer than this is archived by the host's sweep.",
    )

    model_config = SettingsConfigDict(
        env_prefix="DAIMON_REPORT__",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


def load_settings(*, _env_file: str | None = ".env") -> Settings:
    """Construct a Settings from the live process env + optional .env file.

    `_env_file` exists to give tests a way to disable `.env` loading
    (`_env_file=None`) so they only see `monkeypatch.setenv` values.
    """
    return Settings(_env_file=_env_file)  # pyright: ignore[reportCallIssue]
