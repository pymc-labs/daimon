"""Decide what to do with a live MA session whose configuration has drifted.

A session freezes its agent at creation time, so the configuration a caller
wants now and the configuration the session is running can diverge. This module
answers one question, purely: given the snapshot recorded when the session was
created and the snapshot a session created right now would have, do we reuse the
session as it is, update it in place, or replace it?

Three outcomes, by axis (see `session_snapshot`):

- **identity** differs → `ReplaceSession`. MA refuses these on `sessions.update`
  and a reused session would silently keep running the old configuration.
- **mutable** differs → `UpdateInPlace` carrying the ops that apply it. Each op
  is gated on an `MaCapabilities` flag; when the flag is off the change is only
  reachable by replacement, so the decision degrades to `ReplaceSession`.
- nothing differs → `ReuseAsIs`.

The ops describe the whole reuse path, in the order the shell must run them:
`ReplaceEnvFile`, `ReplaceToolsAndMcpServers`, `RotateRepoToken`,
`RemirrorVaultCredentials`. The vault re-mirror is the existing per-turn
credential sync; it is listed only alongside other ops so that one op list
covers the reuse path, and its absence from `ReuseAsIs` is not a signal to skip
it — a caller that re-mirrors on every turn keeps doing so.

Pure module: no I/O and no clock. `now` is injected, and must be timezone-aware.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

from daimon.core.session_snapshot import SessionSnapshot

type ChangeReason = Literal[
    "agent_identity",
    "budget_channel",
    "model",
    "system_prompt",
    "skills",
    "environment",
    "github_mode",
    "repo_set",
    "repo_url",
    "repo_branch",
    "memory_store",
    "memory_access",
    "personal_memory_store",
    "vault",
    "external_caller",
    "tools",
    "mcp_servers",
    "env_file",
    "repo_token_age",
]


@dataclass(frozen=True, slots=True)
class MaCapabilities:
    """What the live MA API lets us change on an existing session.

    Every flag is a probe result (Phase capability matrix). Flipping one off
    degrades the affected change to a session replacement rather than failing.
    """

    env_replaceable_in_place: bool = True
    """A mounted `.env` file resource can be swapped by delete-then-add (P3)."""

    session_agent_updatable: bool = True
    """`sessions.update(agent={tools, mcp_servers})` is accepted (P2)."""

    transcript_readable_after_archive: bool = True
    """`events.list` still works on an archived session (P9.d).

    Not used by the decision; carried here so callers read one capability object.
    """

    repo_token_rotatable: bool = True
    """A GitHub repository resource's authorization token can be rotated (SDK)."""


DEFAULT_MA_CAPABILITIES = MaCapabilities()


@dataclass(frozen=True, slots=True)
class ReplaceToolsAndMcpServers:
    """Send the full desired `tools` and `mcp_servers` arrays to `sessions.update`."""


@dataclass(frozen=True, slots=True)
class RemirrorVaultCredentials:
    """Re-run the existing vault credential sync for the attached vault."""


@dataclass(frozen=True, slots=True)
class RotateAppTokens:
    """Refresh App tokens and swap every mounted repository resource in place."""

    resource_ids: dict[str, str]


@dataclass(frozen=True, slots=True)
class ReplaceEnvFile:
    """Upload the desired `.env`, delete the mounted resource, add the new one."""

    old_resource_id: str | None
    old_file_id: str | None
    mount_path: str = ".env"


type UpdateOp = (
    ReplaceToolsAndMcpServers | RemirrorVaultCredentials | RotateAppTokens | ReplaceEnvFile
)


@dataclass(frozen=True, slots=True)
class ReuseAsIs:
    """The session already runs the desired configuration."""


@dataclass(frozen=True, slots=True)
class UpdateInPlace:
    """The session can keep running once `ops` have been applied to it."""

    ops: tuple[UpdateOp, ...]
    reasons: tuple[ChangeReason, ...]


@dataclass(frozen=True, slots=True)
class ReplaceSession:
    """Only a new session can run the desired configuration."""

    reasons: tuple[ChangeReason, ...]


type CompatibilityDecision = ReuseAsIs | UpdateInPlace | ReplaceSession


UNAVAILABLE_RETRY_AFTER = timedelta(minutes=60)  # First refusal expires after one token lifetime.


def valid_unavailable_repo_urls(recorded: SessionSnapshot, *, now: datetime) -> tuple[str, ...]:
    return tuple(
        url
        for url, since in recorded.unavailable_repos
        if since.tzinfo is not None
        and since.tzinfo.utcoffset(since) is not None
        and now - since < UNAVAILABLE_RETRY_AFTER
    )


def _without(urls: tuple[str, ...], excluded: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(url for url in urls if url not in excluded) if excluded else urls


def identity_change_reasons(
    recorded: SessionSnapshot, desired: SessionSnapshot, *, now: datetime | None = None
) -> tuple[ChangeReason, ...]:
    """Every identity-axis field that differs, in a fixed, user-facing order."""
    excluded = () if now is None else valid_unavailable_repo_urls(recorded, now=now)
    checks: tuple[tuple[bool, ChangeReason], ...] = (
        (recorded.ma_agent_id != desired.ma_agent_id, "agent_identity"),
        (
            recorded.model_id != desired.model_id or recorded.model_speed != desired.model_speed,
            "model",
        ),
        (recorded.system_sha256 != desired.system_sha256, "system_prompt"),
        (recorded.skills_sha256 != desired.skills_sha256, "skills"),
        (recorded.environment_id != desired.environment_id, "environment"),
        (recorded.github_mode != desired.github_mode, "github_mode"),
        (
            # Repos GitHub refused at mint stay excluded instead of replacing the
            # session every turn; reconciliation then removes them from desired.
            recorded.repo_urls != _without(desired.repo_urls, excluded)
            or recorded.token_repo_urls != _without(desired.token_repo_urls, excluded),
            "repo_set",
        ),
        (recorded.repo_url != desired.repo_url, "repo_url"),
        (recorded.repo_branch != desired.repo_branch, "repo_branch"),
        (recorded.memory_store_id != desired.memory_store_id, "memory_store"),
        (recorded.memory_read_only != desired.memory_read_only, "memory_access"),
        (
            # A session running without the caller's personal store keeps
            # running: adding it is no reason to end a live conversation, and
            # the caller's next new session mounts it. One that has a store
            # and should now have another (or none) is replaced, because a
            # stale personal mount is the one memory difference that could
            # expose a person's memory where it no longer belongs.
            recorded.personal_memory_store_id is not None
            and recorded.personal_memory_store_id != desired.personal_memory_store_id,
            "personal_memory_store",
        ),
        (recorded.vault_id != desired.vault_id, "vault"),
        (recorded.is_external != desired.is_external, "external_caller"),
    )
    return tuple(reason for differs, reason in checks if differs)


def decide_session_compatibility(
    *,
    recorded: SessionSnapshot,
    desired: SessionSnapshot,
    capabilities: MaCapabilities,
    now: datetime,
    repo_token_max_age_s: int = 2400,
    force_app_token_rotation: bool = False,
) -> CompatibilityDecision:
    """Reuse, update in place, or replace the session `recorded` describes.

    `desired` carries no resource handles, so the handles the ops need are read
    off `recorded` — the session that actually owns those resources.
    """
    if now.tzinfo is None or now.tzinfo.utcoffset(now) is None:
        raise ValueError("now must be timezone-aware")

    identity_reasons = identity_change_reasons(recorded, desired, now=now)
    if identity_reasons:
        return ReplaceSession(reasons=identity_reasons)

    ops: list[UpdateOp] = []
    reasons: list[ChangeReason] = []
    must_replace = False

    if recorded.env_sha256 != desired.env_sha256:
        reasons.append("env_file")
        if capabilities.env_replaceable_in_place:
            ops.append(
                ReplaceEnvFile(
                    old_resource_id=recorded.env_resource_id,
                    old_file_id=recorded.env_file_id,
                )
            )
        else:
            must_replace = True

    tools_changed = recorded.tools_sha256 != desired.tools_sha256
    mcp_servers_changed = recorded.mcp_servers_sha256 != desired.mcp_servers_sha256
    if tools_changed or mcp_servers_changed:
        if tools_changed:
            reasons.append("tools")
        if mcp_servers_changed:
            reasons.append("mcp_servers")
        if capabilities.session_agent_updatable:
            ops.append(ReplaceToolsAndMcpServers())
        else:
            must_replace = True

    # App tokens live in the vault whether or not a working repo is mounted.
    if recorded.github_mode == "app" and (
        recorded.repo_urls or recorded.token_repo_urls or recorded.repo_token_issued_at is not None
    ):
        issued_at = recorded.repo_token_issued_at
        # Installation tokens are recorded with a conservative 55-minute life.
        # Refresh at 40 minutes, leaving 15 minutes for vault propagation.
        if force_app_token_rotation or issued_at is None or now.timestamp() - issued_at >= 2400:
            reasons.append("repo_token_age")
            if capabilities.repo_token_rotatable:
                ops.append(RotateAppTokens(resource_ids=recorded.repo_resource_ids))
            else:
                must_replace = True

    if must_replace:
        return ReplaceSession(reasons=tuple(reasons))
    if not ops:
        return ReuseAsIs()
    if recorded.vault_id is not None:
        ops.append(RemirrorVaultCredentials())
    return UpdateInPlace(ops=tuple(ops), reasons=tuple(reasons))
