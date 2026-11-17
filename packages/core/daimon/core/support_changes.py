"""Change support limits, menus and the request channel, or settle a request, from any surface.

The CLI, MCP and the web admin call these, as `channel_changes` does for budgets:
authorize `SET_SUPPORT`, take the tenant lock, compare what the caller last
read (`Expected`, else `StaleChannelValue`), write, then record the change in
`channel_changes`. Records never name the person a limit or request is about,
so that table keeps naming no one. Settles lock the limit and the requester only
(`stores.support_escalation`), never the tenant.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import cast

from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.authz import Action, Decision, Place, Subject, authorize
from daimon.core.channel_changes import (
    NEEDS_ADMIN,
    ChannelActor,
    ChannelChangeRefused,
    Expected,
    after_write,
)
from daimon.core.errors import StaleChannelValue
from daimon.core.stores import support as store
from daimon.core.stores import support_escalation as ledger
from daimon.core.stores.access_policy import lock_access_policy
from daimon.core.stores.channel_changes import WORKSPACE_ID, record_change
from daimon.core.stores.identity import find_platform_principal
from daimon.core.stores.support_rows import (
    SupportChannelRow,
    SupportEscalationRow,
    SupportLimitRow,
    SupportMenuRow,
)
from daimon.core.support import (
    LimitSpec,
    MenuSpec,
    SupportScope,
    credits_text,
    format_credits,
    parse_menu_key,
    parse_person_id,
)
from daimon.core.support_routing import settle_channel_ok
from pydantic import JsonValue
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

LIMIT_SETTING = "support_limit"
MENU_SETTING = "support_menu"
MENU_USE_SETTING = "support_menu_use"
CHANNEL_SETTING = "support_channel"
SETTLE_SETTING = "support_settle"
MENU_IN_USE = "A place still uses this menu. Change that place first"
NO_MENU = "No menu has that key."
OWN_CHANNEL_LOCKED = "Requests go to the default channel. Ask whoever runs Daimon."


def may_set_support(subject: Subject) -> Decision:
    """Whether `subject` may change limits, menus and the channel, or settle by hand. Pure."""
    return authorize(
        TenantAccessPolicy(), subject=subject, action=Action.SET_SUPPORT, place=Place()
    )


def _require_admin(actor: ChannelActor) -> None:
    if not may_set_support(actor.subject):
        raise ChannelChangeRefused(NEEDS_ADMIN, kind="forbidden")


def _audit_channel(scope: SupportScope | None, scope_id: str) -> str:
    return scope_id if scope == "channel" else WORKSPACE_ID


async def _record(
    session: AsyncSession,
    actor: ChannelActor,
    *,
    platform: str,
    channel_id: str,
    setting: str,
    before: JsonValue,
    after: JsonValue,
) -> None:
    await record_change(
        session,
        tenant_id=actor.tenant_id,
        platform=platform,
        channel_id=channel_id,
        setting=setting,
        before=before,
        after=after,
        actor_account_id=actor.account_id,
        surface=actor.surface,
    )
    await after_write(session, actor, before != after)


_NOTHING = {
    LIMIT_SETTING: "no limit",
    MENU_SETTING: "no menu",
    MENU_USE_SETTING: "no menu",
    CHANNEL_SETTING: "the default channel",
    SETTLE_SETTING: "no request",
}


def stale_text(exc: StaleChannelValue) -> str:
    """The stale refusal with what is there now, in brief: `... Now: revision 3.`"""
    now = exc.current
    if now is None:
        brief = _NOTHING.get(exc.setting, "nothing")
    elif isinstance(now, str):
        brief = credits_text(Decimal(now))
    else:
        value = cast("dict[str, JsonValue]", now)
        if exc.setting == SETTLE_SETTING:
            charge = value["charge"]
            spent = "" if charge is None else f", {credits_text(Decimal(str(charge)))}"
            brief = f"{value['status']}{spent}"
        elif exc.setting == MENU_USE_SETTING:
            brief = f"menu {value['menu']}"
        else:
            brief = f"revision {value['revision']}"
    return f"{exc} Now: {brief}."


async def missing_menu(
    session: AsyncSession, *, tenant_id: uuid.UUID, key: str
) -> ChannelChangeRefused:
    """The refusal for an unknown menu key, naming the keys there are."""
    keys = [c.key for c in await store.list_menus(session, tenant_id=tenant_id)]
    there = f"Menus: {', '.join(keys)}." if keys else "There are no menus yet."
    return ChannelChangeRefused(f"{NO_MENU} {there}", kind="not_found")


def _stale_check[T](
    setting: str, current: T, expected: Expected[T] | None, as_json: JsonValue
) -> None:
    if expected is not None and expected.value != current:
        raise StaleChannelValue(setting, as_json)


# ---------- Limits ----------


def limit_json(row: SupportLimitRow | None) -> JsonValue:
    """A limit as the audit and stale refusals show it; never the person it is for."""
    if row is None:
        return None

    def credits(value: Decimal | None) -> str | None:
        return None if value is None else format_credits(value)

    return {
        "scope": row.scope,
        "scope_id": row.scope_id,
        "person": row.platform_user_id is not None,
        "shared": credits(row.shared),
        "each_person": credits(row.each_person),
        "window": row.window,
        "starts_at": row.starts_at.isoformat() if row.starts_at else None,
        "ends_at": row.ends_at.isoformat() if row.ends_at else None,
        "revision": row.revision,
    }


def _revision(row: SupportLimitRow | SupportMenuRow | SupportChannelRow | None) -> int | None:
    return None if row is None else row.revision


async def _locked_limit(
    session: AsyncSession,
    actor: ChannelActor,
    *,
    scope: SupportScope,
    scope_id: str,
    platform_user_id: str | None,
    expected: Expected[int | None] | None,
) -> SupportLimitRow | None:
    """The row now, under its charge lock and the tenant lock; `expected` is its revision."""
    peek = await store.get_limit(
        session,
        tenant_id=actor.tenant_id,
        scope=scope,
        scope_id=scope_id,
        platform_user_id=platform_user_id,
    )
    if peek is not None:
        # The limit lock comes before the tenant lock, as in `submit_request`.
        await ledger.lock_limit(session, limit_id=peek.id)
    await lock_access_policy(session, tenant_id=actor.tenant_id)
    current = await store.get_limit(
        session,
        tenant_id=actor.tenant_id,
        scope=scope,
        scope_id=scope_id,
        platform_user_id=platform_user_id,
    )
    if _revision(current) != _revision(peek):
        raise StaleChannelValue(LIMIT_SETTING, limit_json(current))
    _stale_check(LIMIT_SETTING, _revision(current), expected, limit_json(current))
    return current


def _person(platform: str, platform_user_id: str | None) -> str | None:
    return None if platform_user_id is None else parse_person_id(platform, platform_user_id)


async def set_limit(
    sessionmaker: async_sessionmaker[AsyncSession],
    actor: ChannelActor,
    *,
    platform: str,
    spec: LimitSpec,
    expected: Expected[int | None] | None = None,
) -> SupportLimitRow:
    """Set or replace the limit at the spec's place, or a person's own (`parse_limit_spec`)."""
    _require_admin(actor)
    person = _person(platform, spec.platform_user_id)
    async with sessionmaker.begin() as session:
        before = await _locked_limit(
            session,
            actor,
            scope=spec.scope,
            scope_id=spec.scope_id,
            platform_user_id=person,
            expected=expected,
        )
        row = await store.set_limit(
            session,
            tenant_id=actor.tenant_id,
            scope=spec.scope,
            scope_id=spec.scope_id,
            platform_user_id=person,
            shared=spec.shared,
            each_person=spec.each_person,
            window=spec.window,
            starts_at=spec.starts_at,
            ends_at=spec.ends_at,
            set_by_account_id=actor.account_id,
        )
        await _record(
            session,
            actor,
            platform=platform,
            channel_id=_audit_channel(spec.scope, spec.scope_id),
            setting=LIMIT_SETTING,
            before=limit_json(before),
            after=limit_json(row),
        )
    return row


async def clear_limit(
    sessionmaker: async_sessionmaker[AsyncSession],
    actor: ChannelActor,
    *,
    platform: str,
    scope: SupportScope,
    scope_id: str,
    platform_user_id: str | None = None,
    expected: Expected[int | None] | None = None,
) -> bool:
    """Remove a limit; a place limit's pending requests are declined at no charge.

    A request still pending would settle with no limit to bound it, so each is
    declined and audited as a settle by the admin. Settled ones keep their
    charges and count nowhere. A person's own limit has no requests of its own.
    """
    _require_admin(actor)
    person = _person(platform, platform_user_id)
    async with sessionmaker.begin() as session:
        before = await _locked_limit(
            session,
            actor,
            scope=scope,
            scope_id=scope_id,
            platform_user_id=person,
            expected=expected,
        )
        if before is None:
            return False
        declined = (
            await ledger.decline_pending_for_limit(
                session,
                limit_id=before.id,
                settled_by=actor.subject.platform_user_id,
                settled_by_account_id=actor.account_id,
            )
            if person is None
            else []
        )
        await store.delete_limit(session, tenant_id=actor.tenant_id, limit_id=before.id)
        await _record(
            session,
            actor,
            platform=platform,
            channel_id=_audit_channel(scope, scope_id),
            setting=LIMIT_SETTING,
            before=limit_json(before),
            after=None,
        )
        for row in declined:
            await record_change(
                session,
                tenant_id=row.tenant_id,
                platform=row.platform,
                channel_id=row.channel_id,
                setting=SETTLE_SETTING,
                before={"request": str(row.id), "status": "pending", "charge": None},
                after=_request_json(row),
                actor_account_id=actor.account_id,
                surface=actor.surface,
            )
    return True


# ---------- Menus ----------


def _menu_json(row: SupportMenuRow | None) -> JsonValue:
    if row is None:
        return None
    return {"key": row.key, "revision": row.revision, **row.spec.model_dump(mode="json")}


async def _locked_menu(
    session: AsyncSession, actor: ChannelActor, *, key: str, expected: Expected[int | None] | None
) -> SupportMenuRow | None:
    """The menu now, under the tenant lock; `expected` is the revision last read."""
    await lock_access_policy(session, tenant_id=actor.tenant_id)
    current = await store.get_menu(session, tenant_id=actor.tenant_id, key=key)
    _stale_check(MENU_SETTING, _revision(current), expected, _menu_json(current))
    return current


async def set_menu(
    sessionmaker: async_sessionmaker[AsyncSession],
    actor: ChannelActor,
    *,
    platform: str,
    key: str,
    spec: MenuSpec,
    expected: Expected[int | None] | None = None,
) -> SupportMenuRow:
    """Set or replace a menu; places using it offer the new options on their next request."""
    _require_admin(actor)
    key = parse_menu_key(key)
    async with sessionmaker.begin() as session:
        before = await _locked_menu(session, actor, key=key, expected=expected)
        row = await store.set_menu(
            session,
            tenant_id=actor.tenant_id,
            key=key,
            spec=spec,
            set_by_account_id=actor.account_id,
        )
        await _record(
            session,
            actor,
            platform=platform,
            channel_id=WORKSPACE_ID,
            setting=MENU_SETTING,
            before=_menu_json(before),
            after=_menu_json(row),
        )
    return row


async def remove_menu(
    sessionmaker: async_sessionmaker[AsyncSession],
    actor: ChannelActor,
    *,
    platform: str,
    key: str,
    expected: Expected[int | None] | None = None,
) -> None:
    """Remove a menu no place uses; an unknown key is refused."""
    _require_admin(actor)
    async with sessionmaker.begin() as session:
        before = await _locked_menu(session, actor, key=key, expected=expected)
        if before is None:
            raise await missing_menu(session, tenant_id=actor.tenant_id, key=key)
        if await store.menu_places_using(session, tenant_id=actor.tenant_id, menu_id=before.id):
            raise ChannelChangeRefused(MENU_IN_USE, kind="invalid")
        await store.delete_menu(session, tenant_id=actor.tenant_id, key=key)
        await _record(
            session,
            actor,
            platform=platform,
            channel_id=WORKSPACE_ID,
            setting=MENU_SETTING,
            before=_menu_json(before),
            after=None,
        )


async def read_menu_use(
    session: AsyncSession, *, tenant_id: uuid.UUID, scope: SupportScope, scope_id: str
) -> str | None:
    """The key of the menu a place picked itself, as `Expected` compares it."""
    use = await store.get_menu_use(session, tenant_id=tenant_id, scope=scope, scope_id=scope_id)
    if use is None:
        return None
    menu = await store.get_menu_by_id(session, tenant_id=tenant_id, menu_id=use.menu_id)
    return None if menu is None else menu.key


async def set_menu_use(
    sessionmaker: async_sessionmaker[AsyncSession],
    actor: ChannelActor,
    *,
    platform: str,
    scope: SupportScope,
    scope_id: str,
    key: str | None,
    expected: Expected[str | None] | None = None,
) -> str | None:
    """Pick the menu a place offers; `key` None drops the place's own pick. Returns the old key."""
    _require_admin(actor)
    async with sessionmaker.begin() as session:
        await lock_access_policy(session, tenant_id=actor.tenant_id)
        before = await read_menu_use(
            session, tenant_id=actor.tenant_id, scope=scope, scope_id=scope_id
        )
        _stale_check(MENU_USE_SETTING, before, expected, {"menu": before})
        if key is None:
            await store.clear_menu_use(
                session, tenant_id=actor.tenant_id, scope=scope, scope_id=scope_id
            )
        else:
            menu = await store.get_menu(session, tenant_id=actor.tenant_id, key=key)
            if menu is None:
                raise await missing_menu(session, tenant_id=actor.tenant_id, key=key)
            await store.set_menu_use(
                session,
                tenant_id=actor.tenant_id,
                scope=scope,
                scope_id=scope_id,
                menu_id=menu.id,
                set_by_account_id=actor.account_id,
            )
        await _record(
            session,
            actor,
            platform=platform,
            channel_id=_audit_channel(scope, scope_id),
            setting=MENU_USE_SETTING,
            before=None if before is None else {"menu": before},
            after=None if key is None else {"menu": key},
        )
    return before


# ---------- The workspace's own request channel ----------


def _channel_json(row: SupportChannelRow | None) -> JsonValue:
    if row is None:
        return None
    return {"platform": row.platform, "channel_id": row.channel_id, "revision": row.revision}


@dataclass(frozen=True)
class ChannelPick:
    """Where a workspace's requests go; the adapter checked the channel exists."""

    platform: str
    channel_id: str
    slack_team_id: str | None = None
    teams_team_id: str | None = None
    teams_service_url: str | None = None


async def set_support_channel(
    sessionmaker: async_sessionmaker[AsyncSession],
    actor: ChannelActor,
    *,
    pick: ChannelPick | None,
    own_allowed: bool,
    expected: Expected[int | None] | None = None,
) -> SupportChannelRow | None:
    """Set the workspace's own request channel, or drop it (`pick` None: the default applies).

    `own_allowed` is the `support_own_channel` feature (`support_routing.own_channel_allowed`).
    Requests already open stay where they were posted.
    """
    _require_admin(actor)
    if not own_allowed:
        raise ChannelChangeRefused(OWN_CHANNEL_LOCKED, kind="forbidden")
    async with sessionmaker.begin() as session:
        await lock_access_policy(session, tenant_id=actor.tenant_id)
        before = await store.get_support_channel(session, tenant_id=actor.tenant_id)
        _stale_check(CHANNEL_SETTING, _revision(before), expected, _channel_json(before))
        row = None
        if pick is None:
            await store.clear_support_channel(session, tenant_id=actor.tenant_id)
        else:
            row = await store.set_support_channel(
                session,
                tenant_id=actor.tenant_id,
                platform=pick.platform,
                channel_id=pick.channel_id,
                slack_team_id=pick.slack_team_id,
                teams_team_id=pick.teams_team_id,
                teams_service_url=pick.teams_service_url,
                set_by_account_id=actor.account_id,
            )
        await _record(
            session,
            actor,
            platform=pick.platform if pick else (before.platform if before else "web"),
            channel_id=WORKSPACE_ID,
            setting=CHANNEL_SETTING,
            before=_channel_json(before),
            after=_channel_json(row),
        )
    return row


# ---------- Settling ----------


def _request_json(row: SupportEscalationRow | None) -> JsonValue:
    """A request's charge, never its person or words."""
    if row is None:
        return None
    charge = None if row.charge is None else format_credits(row.charge)
    return {"request": str(row.id), "status": row.status, "charge": charge}


async def _record_settle(
    session: AsyncSession,
    *,
    outcome: ledger.SettleOutcome,
    before: SupportEscalationRow,
    actor_account_id: uuid.UUID | None,
    actor: ChannelActor | None,
) -> None:
    if outcome.status not in ("settled", "declined") or outcome.row is None:
        return
    after = outcome.row
    await record_change(
        session,
        tenant_id=after.tenant_id,
        platform=after.platform,
        channel_id=after.channel_id,
        setting=SETTLE_SETTING,
        before=_request_json(before),
        after=_request_json(after),
        actor_account_id=actor_account_id,
        surface="panel" if actor is None else actor.surface,
    )
    if actor is not None:
        await after_write(session, actor, True)


async def settle(
    sessionmaker: async_sessionmaker[AsyncSession],
    actor: ChannelActor,
    *,
    escalation_id: uuid.UUID,
    charge: Decimal | None,
    default_allowance: int,
    expected: Expected[Decimal | None] | None = None,
) -> ledger.SettleOutcome:
    """An admin settles (`charge`) or declines (None) a request of this tenant.

    `expected` is the charge last read: None for a pending request, an amount to
    correct or refund a settled one (down to 0). A mismatch raises
    `StaleChannelValue`. Other refusals come back as the outcome's status.
    """
    _require_admin(actor)
    async with sessionmaker.begin() as session:
        before = await ledger.get_request(
            session, escalation_id=escalation_id, tenant_id=actor.tenant_id
        )
        if before is None:
            return ledger.SettleOutcome(status="not_found", row=None)
        if expected is not None and expected.value is None and before.status != "pending":
            raise StaleChannelValue(SETTLE_SETTING, _request_json(before))
        outcome = await ledger.settle_request(
            session,
            escalation_id=escalation_id,
            charge=charge,
            settled_by=actor.subject.platform_user_id,
            settled_by_account_id=actor.account_id,
            default_allowance=default_allowance,
            tenant_id=actor.tenant_id,
            expected_charge=None if expected is None else expected.value,
        )
        if outcome.status == "stale":
            raise StaleChannelValue(SETTLE_SETTING, _request_json(outcome.row))
        await _record_settle(
            session,
            outcome=outcome,
            before=before,
            actor_account_id=actor.account_id,
            actor=actor,
        )
    return outcome


async def settle_from_post(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    escalation_id: uuid.UUID,
    charge: Decimal | None,
    settled_by: str,
    settled_by_account_id: uuid.UUID | None,
    default_allowance: int,
    channel_id: str,
    parent_id: str | None,
    default_channel_id: str | None,
) -> ledger.SettleOutcome:
    """Anyone in the request's support channel settles from its post, with audit.

    The click must come from the channel the request was posted to (or a thread
    in it); a row from before that was stamped falls back to the workspace's
    channel now (`default_channel_id` is the deployment's). Else "not_here".
    The post may sit in another tenant, so the request is found by id alone. The
    settler's id and account stay on the request (`clear_settler` wipes them on
    a purge). The audit row names an account only of the request's own tenant,
    looked up here: the one passed may belong to the tenant the post sits in.
    """
    async with sessionmaker.begin() as session:
        before = await ledger.get_request(session, escalation_id=escalation_id)
        if before is None:
            return ledger.SettleOutcome(status="not_found", row=None)
        if not await settle_channel_ok(
            session,
            row=before,
            channel_id=channel_id,
            parent_id=parent_id,
            default_channel_id=default_channel_id,
        ):
            return ledger.SettleOutcome(status="not_here", row=before)
        principal = await find_platform_principal(
            session,
            tenant_id=before.tenant_id,
            platform=before.platform,
            external_id=settled_by,
        )
        outcome = await ledger.settle_request(
            session,
            escalation_id=escalation_id,
            charge=charge,
            settled_by=settled_by,
            settled_by_account_id=settled_by_account_id,
            default_allowance=default_allowance,
        )
        await _record_settle(
            session,
            outcome=outcome,
            before=before,
            actor_account_id=None if principal is None else principal.account_id,
            actor=None,
        )
    return outcome


__all__ = [
    "CHANNEL_SETTING",
    "LIMIT_SETTING",
    "MENU_IN_USE",
    "MENU_SETTING",
    "MENU_USE_SETTING",
    "NO_MENU",
    "OWN_CHANNEL_LOCKED",
    "SETTLE_SETTING",
    "ChannelPick",
    "clear_limit",
    "limit_json",
    "may_set_support",
    "missing_menu",
    "read_menu_use",
    "remove_menu",
    "set_limit",
    "set_menu",
    "set_menu_use",
    "set_support_channel",
    "settle",
    "settle_from_post",
    "stale_text",
]
