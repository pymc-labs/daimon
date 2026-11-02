"""A workspace's access settings, shared by the web app, the chat tools and the CLI.

Four settings change who may use Daimon here and how: the invoker allowlist (who
may start turns; empty means everyone, and admins always may), whether people
may move a conversation to a DM, whether turns started from a DM read memory
only, and whether a typed "@<bot name>" starts a turn. Server admins only
(`authorize(SET_WORKSPACE_SETTINGS)`). Each write runs in the caller's
transaction, so a surface records its audit row in the same commit.

The invoker list and DM memory live in the access policy, so they are written
under the policy lock like every other rule; DMs and typed mentions are plain
tenant rows.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from typing import Final

from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.authz import Action, Subject, authorize
from daimon.core.errors import UserFacingError
from daimon.core.stores import direct_messages, tenants
from daimon.core.stores.access_policy import (
    load_access_policy,
    lock_access_policy,
    lock_policy_writes_exclusive,
    set_access_policy,
)
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

#: Platforms with a `/dm-me` command, so the only ones where DM settings mean anything.
DM_PLATFORMS: Final = frozenset({"discord", "slack"})
#: Platforms where a typed "@<bot name>" can be recognised.
MENTION_PLATFORMS: Final = frozenset({"discord", "slack", "teams"})

_ENTRA_OBJECT_ID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
_INVOKER_PATTERNS: Final = {
    "discord": r"[0-9]{15,21}",
    "teams": _ENTRA_OBJECT_ID,
    "slack": r"[UW][A-Z0-9]+",
}


class SettingRefused(UserFacingError):
    """The change was not made; the message says why in plain words."""


@dataclass(frozen=True)
class WorkspaceAccess:
    """What the access settings say now."""

    invoker_user_ids: tuple[str, ...]
    """Empty: everyone may start a turn."""
    dm_enabled: bool
    dm_memory_read_only: bool
    plain_text_mentions: bool


def parse_invokers(
    values: Sequence[str], *, platform: str, trusted: Collection[str] = ()
) -> tuple[str, ...]:
    """The allowlist `values` stand for, trimmed and without repeats.

    Each id must look like a user id of `platform`. `trusted` are ids the caller
    already resolved to a person of this workspace (a web person's own id), which
    need not look like a chat id. An empty list is everyone.
    """
    cleaned: list[str] = []
    for value in values:
        entry = value.strip()
        pattern = _INVOKER_PATTERNS.get(platform)
        if not entry or (
            entry not in trusted and pattern is not None and re.fullmatch(pattern, entry) is None
        ):
            raise SettingRefused(f"{value!r} is not a valid {platform} user id. Nothing changed.")
        cleaned.append(entry)
    return tuple(dict.fromkeys(cleaned))


def _require_admin(policy: TenantAccessPolicy, subject: Subject) -> None:
    if not authorize(policy, subject=subject, action=Action.SET_WORKSPACE_SETTINGS):
        raise SettingRefused("Only an admin can change this. Nothing changed.")


async def load_access(session: AsyncSession, *, tenant_id: uuid.UUID) -> WorkspaceAccess:
    """The four settings, read together."""
    policy = await load_access_policy(session, tenant_id=tenant_id)
    return WorkspaceAccess(
        invoker_user_ids=policy.invoker_user_ids,
        dm_enabled=await direct_messages.dm_enabled(session, tenant_id=tenant_id),
        dm_memory_read_only=policy.dm_memory_read_only,
        plain_text_mentions=await _mentions_on(session, tenant_id),
    )


async def _mentions_on(session: AsyncSession, tenant_id: uuid.UUID) -> bool:
    row = await tenants.get_tenant(session, tenant_id)
    return row is not None and row.plain_text_mentions


async def _change_policy(
    session: AsyncSession, *, tenant_id: uuid.UUID, subject: Subject, **changes: object
) -> TenantAccessPolicy:
    """Merge `changes` into the stored policy under the policy lock, as an admin."""
    await lock_policy_writes_exclusive(session, tenant_id=tenant_id)
    await lock_access_policy(session, tenant_id=tenant_id)
    current = await load_access_policy(session, tenant_id=tenant_id)
    _require_admin(current, subject)
    try:
        updated = TenantAccessPolicy.model_validate(current.model_dump() | changes)
    except ValidationError as exc:
        raise SettingRefused("That value isn't allowed. Nothing changed.") from exc
    if updated != current:
        await set_access_policy(session, tenant_id=tenant_id, policy=updated)
    return updated


async def set_invokers(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    subject: Subject,
    platform: str,
    user_ids: Sequence[str],
    trusted: Collection[str] = (),
) -> tuple[str, ...]:
    """Replace who may start turns; an empty list lets everyone. Returns the stored list."""
    parsed = parse_invokers(user_ids, platform=platform, trusted=trusted)
    policy = await _change_policy(
        session, tenant_id=tenant_id, subject=subject, invoker_user_ids=parsed
    )
    return policy.invoker_user_ids


async def set_dm_memory_read_only(
    session: AsyncSession, *, tenant_id: uuid.UUID, subject: Subject, enabled: bool
) -> bool:
    """Give turns started from a DM read-only memory (or not)."""
    policy = await _change_policy(
        session, tenant_id=tenant_id, subject=subject, dm_memory_read_only=enabled
    )
    return policy.dm_memory_read_only


async def set_dms(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    subject: Subject,
    platform: str,
    enabled: bool,
) -> None:
    """Allow or stop moving conversations to a DM. Discord and Slack only."""
    if platform not in DM_PLATFORMS:
        raise SettingRefused("Private chats aren't available here. Nothing changed.")
    _require_admin(await load_access_policy(session, tenant_id=tenant_id), subject)
    await direct_messages.set_dm_enabled(session, tenant_id=tenant_id, enabled=enabled)


async def set_plain_text_mentions(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    subject: Subject,
    platform: str,
    enabled: bool,
) -> None:
    """Let a typed "@<bot name>" start a turn, workspace-wide."""
    if platform not in MENTION_PLATFORMS:
        raise SettingRefused("Typed mentions aren't available here. Nothing changed.")
    _require_admin(await load_access_policy(session, tenant_id=tenant_id), subject)
    await tenants.set_plain_text_mentions(session, tenant_id=tenant_id, enabled=enabled)
