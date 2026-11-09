"""A workspace's access settings, shared by the web app, the chat tools and the CLI.

Four settings change who may use Daimon here and how: the invoker allowlist (who
may start turns; empty means everyone, and admins always may), whether people
may move a conversation to a DM, whether turns started from a DM read memory
only, and whether a typed "@<bot name>" starts a turn. Server admins only
(`authorize(SET_WORKSPACE_SETTINGS)`). Each write runs in the caller's
transaction, so a surface records its audit row in the same commit.

The invoker list lives in the access policy, so it is written under the policy lock like
every other rule; DMs, DM memory and typed mentions are workspace-tier feature values
(`direct_messages`, `dm_memory_read_only`, `plain_text_mentions`), written through the
feature service so they are recorded like any other change.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass
from typing import Final

from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.authz import Action, Subject, authorize
from daimon.core.errors import UserFacingError
from daimon.core.features.service import set_workspace_feature
from daimon.core.features.snapshot import FeaturePlace, feature_in
from daimon.core.stores import identity, web_people
from daimon.core.stores.access_policy import (
    load_access_policy,
    lock_access_policy,
    lock_policy_writes_exclusive,
    set_access_policy,
)
from daimon.core.stores.domain import ChangeSurface, WebPersonRow
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
        dm_enabled=await _on(session, tenant_id, "direct_messages"),
        dm_memory_read_only=await _on(session, tenant_id, "dm_memory_read_only"),
        plain_text_mentions=await _on(session, tenant_id, "plain_text_mentions"),
    )


async def _on(session: AsyncSession, tenant_id: uuid.UUID, key: str) -> bool:
    """A workspace-wide setting; "discord" stands for any platform that offers it."""
    return (await feature_in(session, None, FeaturePlace(tenant_id, "discord"), key)).on


async def _set(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    platform: str,
    key: str,
    enabled: bool,
    account_id: uuid.UUID | None,
    surface: ChangeSurface,
) -> None:
    await set_workspace_feature(
        session,
        None,
        tenant_id=tenant_id,
        platform=platform,
        key=key,
        value="on" if enabled else "off",
        account_id=account_id,
        surface=surface,
    )


async def _change_policy(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    subject: Subject,
    build: Callable[[TenantAccessPolicy], dict[str, object]],
) -> TenantAccessPolicy:
    """Merge what `build` returns for the stored policy, under the policy lock, as an admin."""
    await lock_policy_writes_exclusive(session, tenant_id=tenant_id)
    await lock_access_policy(session, tenant_id=tenant_id)
    current = await load_access_policy(session, tenant_id=tenant_id)
    _require_admin(current, subject)
    try:
        updated = TenantAccessPolicy.model_validate(current.model_dump() | build(current))
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
        session, tenant_id=tenant_id, subject=subject, build=lambda _: {"invoker_user_ids": parsed}
    )
    return policy.invoker_user_ids


async def set_dm_memory_read_only(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    subject: Subject,
    enabled: bool,
    account_id: uuid.UUID | None = None,
    surface: ChangeSurface = "web",
) -> bool:
    """Give turns started from a DM read-only memory (or not)."""
    _require_admin(await load_access_policy(session, tenant_id=tenant_id), subject)
    await _set(
        session,
        tenant_id=tenant_id,
        platform="discord",
        key="dm_memory_read_only",
        enabled=enabled,
        account_id=account_id,
        surface=surface,
    )
    return enabled


async def set_dms(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    subject: Subject,
    platform: str,
    enabled: bool,
    account_id: uuid.UUID | None = None,
    surface: ChangeSurface = "chat",
) -> None:
    """Allow or stop moving conversations to a DM. Discord and Slack only."""
    if platform not in DM_PLATFORMS:
        raise SettingRefused("Private chats aren't available here. Nothing changed.")
    _require_admin(await load_access_policy(session, tenant_id=tenant_id), subject)
    await _set(
        session,
        tenant_id=tenant_id,
        platform=platform,
        key="direct_messages",
        enabled=enabled,
        account_id=account_id,
        surface=surface,
    )


async def set_plain_text_mentions(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    subject: Subject,
    platform: str,
    enabled: bool,
    account_id: uuid.UUID | None = None,
    surface: ChangeSurface = "chat",
) -> None:
    """Let a typed "@<bot name>" start a turn, workspace-wide."""
    if platform not in MENTION_PLATFORMS:
        raise SettingRefused("Typed mentions aren't available here. Nothing changed.")
    _require_admin(await load_access_policy(session, tenant_id=tenant_id), subject)
    await _set(
        session,
        tenant_id=tenant_id,
        platform=platform,
        key="plain_text_mentions",
        enabled=enabled,
        account_id=account_id,
        surface=surface,
    )


@dataclass(frozen=True)
class InvokerPeople:
    """The invoker allowlist as people: who it names, and how many ids name nobody here."""

    people: tuple[WebPersonRow, ...]
    others: int
    """Ids of people who never signed in to the web app; the web app keeps them."""

    @property
    def limited(self) -> bool:
        return bool(self.people) or self.others > 0


async def _ids_by_person(
    session: AsyncSession, *, tenant_id: uuid.UUID
) -> dict[uuid.UUID, tuple[WebPersonRow, frozenset[str]]]:
    """Every id a person's turns can run as: their own, and each linked chat account."""
    people = await web_people.list_people(session, tenant_id=tenant_id)
    linked = await identity.list_platform_principals_for_accounts(
        session, tenant_id=tenant_id, account_ids={p.account_id for p in people}
    )
    ids: dict[uuid.UUID, set[str]] = {p.account_id: set() for p in people}
    for principal in linked:
        ids[principal.account_id].add(principal.external_id)
    return {p.id: (p, frozenset({str(p.id), *ids[p.account_id]})) for p in people}


def _covered(known: dict[uuid.UUID, tuple[WebPersonRow, frozenset[str]]]) -> set[str]:
    return {i for _, ids in known.values() for i in ids}


def _named(
    invokers: Collection[str], known: dict[uuid.UUID, tuple[WebPersonRow, frozenset[str]]]
) -> InvokerPeople:
    listed = set(invokers)
    named = tuple(person for person, ids in known.values() if ids & listed)
    covered = _covered(known)
    return InvokerPeople(people=named, others=len(listed - covered))


async def load_invoker_people(session: AsyncSession, *, tenant_id: uuid.UUID) -> InvokerPeople:
    """Who the invoker allowlist names, among the workspace's web people."""
    policy = await load_access_policy(session, tenant_id=tenant_id)
    known = await _ids_by_person(session, tenant_id=tenant_id)
    return _named(policy.invoker_user_ids, known)


async def set_invoker_people(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    subject: Subject,
    person_ids: Sequence[uuid.UUID] | None,
) -> InvokerPeople:
    """Limit who may start turns to these people; None lets everyone.

    Each person is listed by every id their turns can run as, so the limit holds in
    the web app and in chat. Ids of people the web app doesn't know stay listed.
    """
    known = await _ids_by_person(session, tenant_id=tenant_id)
    chosen = list(dict.fromkeys(person_ids or ()))
    if any(person_id not in known for person_id in chosen):
        raise SettingRefused("Pick people from this workspace. Nothing changed.")

    def build(current: TenantAccessPolicy) -> dict[str, object]:
        if person_ids is None:
            return {"invoker_user_ids": ()}
        covered = _covered(known)
        kept = [i for i in current.invoker_user_ids if i not in covered]
        added = sorted(i for person_id in chosen for i in known[person_id][1])
        listed = tuple(dict.fromkeys([*kept, *added]))
        if not listed:
            raise SettingRefused("Pick at least one person, or let everyone. Nothing changed.")
        return {"invoker_user_ids": listed}

    policy = await _change_policy(session, tenant_id=tenant_id, subject=subject, build=build)
    return _named(policy.invoker_user_ids, known)
