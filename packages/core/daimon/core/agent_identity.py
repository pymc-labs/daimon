"""Per-message identity for a daimon agent."""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

import structlog
from daimon.core.config import Settings
from daimon.core.defaults.metadata import MA_METADATA_KEY_MANAGED
from daimon.core.stores.agent_avatars import (
    get_agent_avatar,
    get_or_create_avatar,
    normalize_agent_name,
)
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

log = structlog.get_logger(__name__)
_face_tasks: dict[tuple[uuid.UUID, str], asyncio.Task[None]] = {}
_face_failures: dict[tuple[uuid.UUID, str], tuple[int, float]] = {}
_face_started: dict[tuple[uuid.UUID, str], float] = {}
_MAX_FACE_ATTEMPTS = 3
# A new agent's first answer waits this long after the render started (it
# takes about a second), so it does not go out without a picture. The wait
# runs from the render's start, so a stuck render costs later turns nothing.
_FIRST_FACE_WAIT_S = 3.0

# Shown wherever a stale control or form tries to upload an agent picture.
CUSTOM_PICTURES_OFF = "Custom pictures are turned off. Each agent uses its generated face."


@dataclass(frozen=True)
class AgentIdentity:
    name: str
    avatar_url: str | None
    builtin: bool


def identity_enabled_for(
    settings: Settings,
    platform: Literal["discord", "slack", "teams"],
    workspace_id: str | int | None,
) -> bool:
    """Apply the deployment switch and workspace exclusions when an ID is known.

    A platform DM has no guild or workspace ID, so it follows the global switch.
    """
    identity = getattr(settings, "agent_identity", None)
    if identity is None:
        return False
    if identity.enabled is not True:
        return False
    if platform == "discord":
        excluded = getattr(identity, "excluded_discord_guild_ids", ())
    elif platform == "slack":
        excluded = getattr(identity, "excluded_slack_team_ids", ())
    else:
        return True
    if not isinstance(excluded, (list, tuple, set, frozenset)):
        excluded = ()
    if workspace_id is None:
        return True
    return str(workspace_id) not in excluded


def is_builtin_agent(
    *, name: str, metadata: Mapping[str, str] | None, default_agent_name: str | None
) -> bool:
    """Identify the managed deployment agent without assuming its display name."""
    return (metadata or {}).get(MA_METADATA_KEY_MANAGED) == "true" or (
        default_agent_name is not None and name == default_agent_name
    )


async def resolve_agent_identity(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    agent_name: str,
    is_builtin: bool,
    public_base_url: str | None,
    enabled: bool = False,
    background_sessionmaker: async_sessionmaker[AsyncSession] | None = None,
    wait_for_face: bool = False,
) -> AgentIdentity:
    """Resolve the identity once when a turn admits an agent.

    Built-in turns keep the platform app's own name and icon. `wait_for_face`
    is for answer paths only: panels with a platform deadline must not wait.
    """
    if is_builtin or not enabled:
        return AgentIdentity(name=agent_name, avatar_url=None, builtin=True)
    avatar = None
    try:
        avatar = await get_agent_avatar(session, tenant_id=tenant_id, agent_name=agent_name)
        if (avatar is None or (avatar.source == "default" and not avatar.has_face_assignment)) and (
            background_sessionmaker is not None
        ):
            task = _schedule_face(
                background_sessionmaker, tenant_id=tenant_id, agent_name=agent_name
            )
            key = tenant_id, normalize_agent_name(agent_name)
            if task is not None and wait_for_face:
                started = _face_started.get(key, time.monotonic())
                remaining = max(0.0, started + _FIRST_FACE_WAIT_S - time.monotonic())
                done, _ = await asyncio.wait({task}, timeout=remaining)
                if task in done and key not in _face_failures:
                    avatar = await get_agent_avatar(
                        session, tenant_id=tenant_id, agent_name=agent_name
                    )
    except Exception as exc:
        log.warning("agent_identity.avatar_lookup_failed", error_type=type(exc).__name__)
    base = public_base_url.rstrip("/") if public_base_url else None
    url = f"{base}/avatars/{avatar.token}/{avatar.sha256[:12]}.png" if base and avatar else None
    return AgentIdentity(name=agent_name, avatar_url=url, builtin=False)


def _schedule_face(
    sessionmaker: async_sessionmaker[AsyncSession], *, tenant_id: uuid.UUID, agent_name: str
) -> asyncio.Task[None] | None:
    """Start the face render once per agent; return the running task, if any."""
    key = tenant_id, normalize_agent_name(agent_name)
    if key in _face_tasks:
        return _face_tasks[key]
    attempts, next_retry = _face_failures.get(key, (0, 0.0))
    if attempts >= _MAX_FACE_ATTEMPTS or time.monotonic() < next_retry:
        return None
    bind = sessionmaker.kw.get("bind")
    # A checked-out connection cannot serve the turn and the generator at
    # once. Bind background work to its engine, preserving session metadata.
    background_factory = (
        async_sessionmaker(
            bind if isinstance(bind, AsyncEngine) else bind.engine,
            expire_on_commit=False,
            info=sessionmaker.kw.get("info"),
        )
        if bind is not None
        else sessionmaker
    )

    async def generate() -> None:
        try:
            async with background_factory.begin() as session:
                await get_or_create_avatar(
                    session, tenant_id=tenant_id, agent_name=agent_name, face_enabled=True
                )
            _face_failures.pop(key, None)
        except Exception as exc:
            failed = _face_failures.get(key, (0, 0.0))[0] + 1
            _face_failures[key] = (
                failed,
                float("inf") if failed >= _MAX_FACE_ATTEMPTS else time.monotonic() + 30 * 2**failed,
            )
            if failed == 1:
                log.error(
                    "agent_identity.avatar_generation_failed",
                    error_type=type(exc).__name__,
                    exc_info=True,
                )
            else:
                log.warning(
                    "agent_identity.avatar_generation_failed",
                    error_type=type(exc).__name__,
                    attempt=failed,
                )

    task = asyncio.create_task(generate(), name="agent-face-generation")
    _face_tasks[key] = task
    _face_started[key] = time.monotonic()

    def finished(_: object) -> None:
        _face_tasks.pop(key, None)
        _face_started.pop(key, None)

    task.add_done_callback(finished)
    return task
