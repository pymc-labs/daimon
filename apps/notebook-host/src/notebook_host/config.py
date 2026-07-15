"""pydantic-settings for notebook-host.

Constructed via `load_settings()`. Never import a module-level settings
singleton — construct once at the edge (app startup, test fixture) and
inject downstream.

Env prefix: DAIMON_NOTEBOOK__  (flat — no nested delimiter needed here,
all fields are top-level on the single Settings class).
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    data_dir: Path = Path("/data/notebooks")
    # Bearer tokens accepted on admin routes. Provide MORE than one to rotate
    # without downtime: add the new one, deploy bot with new value, drop the
    # old. ``DAIMON_NOTEBOOK__ADMIN_SECRETS=primary,backup`` is the canonical
    # form. ``DAIMON_NOTEBOOK__ADMIN_SECRET`` (singular) is still accepted —
    # auto-wrapped into a single-element list — so existing deploys don't
    # break on upgrade.
    admin_secrets: Annotated[list[SecretStr], NoDecode] = Field(default_factory=list[SecretStr])
    # Deprecated single-bearer alias. Set neither = ValidationError; setting
    # both = both are accepted (singular appended to the list).
    admin_secret: SecretStr | None = None

    @field_validator("admin_secrets", mode="before")
    @classmethod
    def _split_admin_secrets(cls, v: object) -> object:
        # pydantic-settings reads env as a string; comma-split into a list.
        if isinstance(v, str):
            return [s.strip() for s in v.split(",") if s.strip()]
        return v

    @model_validator(mode="after")
    def _merge_legacy_admin_secret(self) -> Settings:
        """Fold the singular ADMIN_SECRET into the list; require at least one."""
        if self.admin_secret is not None:
            # The model is frozen-by-default for the duration of validators,
            # so we mutate the list in place — Pydantic copies it on access.
            secrets_list = list(self.admin_secrets)
            secrets_list.append(self.admin_secret)
            object.__setattr__(self, "admin_secrets", secrets_list)
        if not self.admin_secrets:
            raise ValueError(
                "at least one admin bearer required: set "
                "DAIMON_NOTEBOOK__ADMIN_SECRETS (CSV) or DAIMON_NOTEBOOK__ADMIN_SECRET"
            )
        return self

    host_port: int = 8001
    marimo_port_start: int = 8100
    marimo_port_end: int = 8160
    # How long a published notebook subprocess lives before the sweeper reaps
    # it. Each live notebook holds a port (marimo_port_start..end) and up to
    # marimo_rlimit_as_bytes of memory, so the TTL is what reclaims those from
    # abandoned notebooks. Set to 0 (or any value <= 0) to disable age-based
    # reaping entirely — notebooks then live until their kernel dies or they're
    # explicitly deleted (expires_at comes back null). Indefinite trades the
    # automatic reclamation away: alive-but-abandoned notebooks accumulate until
    # the port pool or memory is exhausted, so only set it where notebook churn
    # is low and you're prepared to delete manually.
    subprocess_ttl_seconds: int = 86400
    sweep_interval_seconds: int = 300
    spawn_timeout_seconds: float = 20.0
    # Before serving a published notebook, run `marimo export` to confirm its
    # cells execute (catches the silent MultipleDefinitionError that ships a
    # broken notebook). Disable to save the extra subprocess per publish.
    validate_on_publish: bool = True
    # Wall-clock budget for that validation export. A notebook that exceeds it
    # is published anyway (assumed slow, not broken) — the structural errors
    # this targets fail near-instantly, so the timeout only catches heavy
    # compute (e.g. large pm.sample), which we don't want to block on.
    validation_timeout_seconds: float = 60.0
    public_host: str = "localhost"
    # External URL prefix returned to clients in publish responses. Set this
    # when the host is reached through a TLS terminator that strips the
    # internal port (e.g. Fly's https edge → internal :8001). When set, it
    # replaces the `http://<public_host>:<host_port>` prefix verbatim — no
    # scheme/port mangling. Example:
    #   DAIMON_NOTEBOOK__PUBLIC_URL_BASE="https://daimon-notebook-host.fly.dev"
    # Leave unset for local / trusted-network deployments where the
    # host_port is part of the address users actually visit.
    public_url_base: str | None = None
    # Max bytes for the published notebook source. 1 MiB is far past a
    # hand-written notebook; agents emitting megabytes of generated code are
    # the failure mode this catches. Pre-allocation defense rather than a
    # post-hoc disk-fill guard.
    max_source_bytes: int = 1_048_576
    # Host-side hard ceiling on attachment PUT body size. Defense-in-depth
    # against a buggy / malicious daimon that overflows its own
    # ``max_attachment_bytes`` cap (operator policy lives on the daimon side).
    # 100 MiB at default — well above the 10 MiB daimon cap, well below disk-fill.
    max_attachment_bytes_ceiling: int = 100 * 1024 * 1024
    # Per-subprocess RLIMIT_AS (address space) in bytes. Default 4 GiB —
    # generous enough for ML-ish notebooks, tight enough that a runaway
    # alloc kills only the offender, not the Fly Machine. Linux only;
    # macOS dev hosts skip the setrlimit call.
    marimo_rlimit_as_bytes: int = 4 * 1024 * 1024 * 1024
    # Per-subprocess RLIMIT_CPU (cumulative CPU seconds). Default 3600 =
    # 1 hour before SIGXCPU. Notebooks doing legitimate long-running compute
    # may need this raised. 0 disables the cap.
    marimo_rlimit_cpu_seconds: int = 3600
    # Path to the orphan-recovery pids file. Host writes {slug: PidRecord}
    # on every register/unregister; at startup, reads + reaps any live
    # PIDs that survived the previous host crash. ``None`` (default) means
    # "use ``data_dir / 'pids.json'``" — keeps both on the same volume.
    pids_file: Path | None = None
    # Path to the persistent-blog registry. Host writes {slug: BlogRecord} on
    # blog publish/delete; at startup it reads this to respawn each blog in run
    # mode. ``None`` (default) means "use ``data_dir / 'blogs.json'``" — kept on
    # the same persistent volume as the source files.
    blogs_file: Path | None = None

    @property
    def resolved_pids_file(self) -> Path:
        """The actual pids.json path: explicit setting, else data_dir / pids.json."""
        return self.pids_file if self.pids_file is not None else self.data_dir / "pids.json"

    @property
    def resolved_blogs_file(self) -> Path:
        """The actual blogs.json path: explicit setting, else data_dir / blogs.json."""
        return self.blogs_file if self.blogs_file is not None else self.data_dir / "blogs.json"

    # WebSocket Origin allowlist. Empty list = check disabled (suitable for
    # trusted-network deployments where the host isn't browser-reachable
    # from outside). Set to e.g. ["https://nbs.example.com"] to enforce.
    # Env: DAIMON_NOTEBOOK__ALLOWED_ORIGINS="https://a.example.com,https://b.example.com"
    # NoDecode prevents pydantic-settings from trying to JSON-parse the env
    # value before our CSV-splitter validator runs.
    allowed_origins: Annotated[list[str], NoDecode] = Field(default_factory=list[str])

    @field_validator("allowed_origins", mode="before")
    @classmethod
    def _split_origins(cls, v: object) -> object:
        # pydantic-settings reads env as a string; comma-split into a list.
        if isinstance(v, str):
            return [o.strip() for o in v.split(",") if o.strip()]
        return v

    model_config = SettingsConfigDict(
        env_prefix="DAIMON_NOTEBOOK__",
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
