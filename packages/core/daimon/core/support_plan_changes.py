"""Change support plans, catalogs and allowances, or settle a request, from any surface.

The CLI, MCP and the web admin call these, as `channel_changes` does for budgets:
authorize `SET_SUPPORT_PLAN`, take the tenant lock, compare what the caller last
read (`Expected`, else `StaleChannelValue`), write, then record the change in
`channel_changes`. Records never name the person an allowance or request is about,
so that table keeps naming no one. Settles lock the plan and the requester only
(`stores.support_escalation`), never the tenant.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
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
from daimon.core.stores import support_escalation as ledger
from daimon.core.stores import support_plans as store
from daimon.core.stores.access_policy import lock_access_policy
from daimon.core.stores.channel_changes import WORKSPACE_ID, record_change
from daimon.core.stores.identity import find_platform_principal
from daimon.core.stores.support_rows import (
    SupportAllowanceRow,
    SupportCatalogRow,
    SupportEscalationRow,
    SupportPlanRow,
)
from daimon.core.support_plans import (
    PlanScope,
    PlanSpec,
    SupportCatalogSpec,
    credits_text,
    format_credits,
    parse_catalog_key,
    parse_credits,
    parse_person_id,
)
from daimon.core.windows import BudgetWindow
from pydantic import JsonValue
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

PLAN_SETTING = "support_plan"
CATALOG_SETTING = "support_catalog"
ALLOWANCE_SETTING = "support_allowance"
SETTLE_SETTING = "support_settle"
CATALOG_IN_USE = "A plan still uses this menu. Change that plan first."
NO_PLAN = "There is no plan there."


def may_set_support_plan(subject: Subject) -> Decision:
    """Whether `subject` may change plans, menus and allowances, or settle by hand. Pure."""
    return authorize(
        TenantAccessPolicy(), subject=subject, action=Action.SET_SUPPORT_PLAN, place=Place()
    )


def _require_admin(actor: ChannelActor) -> None:
    if not may_set_support_plan(actor.subject):
        raise ChannelChangeRefused(NEEDS_ADMIN, kind="forbidden")


def is_support_channel(*, configured: str | None, channel_id: str, parent_id: str | None) -> bool:
    """A settle click counts only from the deployment's support channel or a thread in it."""
    return configured is not None and configured in (channel_id, parent_id)


def _audit_channel(scope: PlanScope | None, scope_id: str) -> str:
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
    PLAN_SETTING: "no plan",
    CATALOG_SETTING: "no menu",
    ALLOWANCE_SETTING: "no own cap",
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
        if exc.setting == CATALOG_SETTING:
            brief = f"revision {value['revision']}"
        elif exc.setting == SETTLE_SETTING:
            charge = value["charge"]
            spent = "" if charge is None else f", {credits_text(Decimal(str(charge)))}"
            brief = f"{value['status']}{spent}"
        else:
            parts = [f"menu {value['catalog'] or 'default'}"]
            if value["pool"] is not None:
                parts.append(f"pool {value['pool']}")
            if value["per_person"] is not None:
                parts.append(f"{value['per_person']} per person")
            brief = ", ".join([*parts, str(value["window"])])
    return f"{exc} Now: {brief}."


async def missing_catalog(
    session: AsyncSession, *, tenant_id: uuid.UUID, key: str
) -> ChannelChangeRefused:
    """The refusal for an unknown menu key, naming the keys there are."""
    keys = [c.key for c in await store.list_catalogs(session, tenant_id=tenant_id)]
    there = f"Menus: {', '.join(keys)}." if keys else "There are no menus yet."
    return ChannelChangeRefused(f"No menu has the key {key}. {there}", kind="not_found")


def _stale_check[T](
    setting: str, current: T, expected: Expected[T] | None, as_json: JsonValue
) -> None:
    if expected is not None and expected.value != current:
        raise StaleChannelValue(setting, as_json)


# ---------- Plans ----------


@dataclass(frozen=True)
class PlanValue:
    """A plan as a caller reads and compares it; `catalog` None is the default menu."""

    scope: PlanScope
    scope_id: str
    catalog: str | None
    pool: Decimal | None
    per_person: Decimal | None
    window: BudgetWindow
    starts_at: datetime | None = None
    ends_at: datetime | None = None

    def as_json(self) -> dict[str, JsonValue]:
        def credits(value: Decimal | None) -> str | None:
            return None if value is None else format_credits(value)

        return {
            "scope": self.scope,
            "scope_id": self.scope_id,
            "catalog": self.catalog,
            "pool": credits(self.pool),
            "per_person": credits(self.per_person),
            "window": self.window,
            "starts_at": self.starts_at.isoformat() if self.starts_at else None,
            "ends_at": self.ends_at.isoformat() if self.ends_at else None,
        }


def _plan_json(value: PlanValue | None) -> JsonValue:
    return None if value is None else value.as_json()


async def read_plan(
    session: AsyncSession, *, tenant_id: uuid.UUID, scope: PlanScope, scope_id: str
) -> PlanValue | None:
    """The plan at (scope, scope_id) as `Expected` compares it."""
    row = await store.get_plan(session, tenant_id=tenant_id, scope=scope, scope_id=scope_id)
    return await _plan_value(session, row)


async def _plan_value(session: AsyncSession, row: SupportPlanRow | None) -> PlanValue | None:
    if row is None:
        return None
    catalog = None
    if row.catalog_id is not None:
        found = await store.get_catalog_by_id(
            session, tenant_id=row.tenant_id, catalog_id=row.catalog_id
        )
        catalog = None if found is None else found.key
    return PlanValue(
        row.scope,
        row.scope_id,
        catalog,
        row.pool,
        row.per_person,
        row.window,
        row.starts_at,
        row.ends_at,
    )


async def _locked_plan(
    session: AsyncSession,
    actor: ChannelActor,
    *,
    scope: PlanScope,
    scope_id: str,
    expected: Expected[PlanValue | None] | None,
) -> PlanValue | None:
    await lock_access_policy(session, tenant_id=actor.tenant_id)
    current = await read_plan(session, tenant_id=actor.tenant_id, scope=scope, scope_id=scope_id)
    _stale_check(PLAN_SETTING, current, expected, _plan_json(current))
    return current


async def set_plan(
    sessionmaker: async_sessionmaker[AsyncSession],
    actor: ChannelActor,
    *,
    platform: str,
    spec: PlanSpec,
    expected: Expected[PlanValue | None] | None = None,
) -> SupportPlanRow:
    """Set or replace the plan at the spec's place (`parse_plan_spec` validated `spec`)."""
    _require_admin(actor)
    async with sessionmaker.begin() as session:
        before = await _locked_plan(
            session, actor, scope=spec.scope, scope_id=spec.scope_id, expected=expected
        )
        catalog_id = None
        if spec.catalog is not None:
            catalog = await store.get_catalog(session, tenant_id=actor.tenant_id, key=spec.catalog)
            if catalog is None:
                raise await missing_catalog(session, tenant_id=actor.tenant_id, key=spec.catalog)
            catalog_id = catalog.id
        row = await store.set_plan(
            session,
            tenant_id=actor.tenant_id,
            platform=platform,
            scope=spec.scope,
            scope_id=spec.scope_id,
            catalog_id=catalog_id,
            pool=spec.pool,
            per_person=spec.per_person,
            window=spec.window,
            set_by_account_id=actor.account_id,
        )
        await _record(
            session,
            actor,
            platform=platform,
            channel_id=_audit_channel(spec.scope, spec.scope_id),
            setting=PLAN_SETTING,
            before=_plan_json(before),
            after=_plan_json(await _plan_value(session, row)),
        )
    return row


async def clear_plan(
    sessionmaker: async_sessionmaker[AsyncSession],
    actor: ChannelActor,
    *,
    platform: str,
    scope: PlanScope,
    scope_id: str,
    expected: Expected[PlanValue | None] | None = None,
) -> bool:
    """Remove the plan; its allowances go with it, its pending requests are declined.

    A request still pending would settle with no plan to bound it, so each is
    declined at no charge and audited as a settle by the admin. Settled ones
    keep their charges and count nowhere.
    """
    _require_admin(actor)
    async with sessionmaker.begin() as session:
        # The plan lock comes before the tenant lock, as in `submit_request`.
        peek = await store.get_plan(
            session, tenant_id=actor.tenant_id, scope=scope, scope_id=scope_id
        )
        if peek is not None:
            await ledger.lock_plan(session, plan_id=peek.id)
        before = await _locked_plan(
            session, actor, scope=scope, scope_id=scope_id, expected=expected
        )
        current = await store.get_plan(
            session, tenant_id=actor.tenant_id, scope=scope, scope_id=scope_id
        )
        if (None if current is None else current.id) != (None if peek is None else peek.id):
            raise StaleChannelValue(PLAN_SETTING, _plan_json(before))
        declined = (
            []
            if current is None
            else await ledger.decline_pending_for_plan(
                session,
                plan_id=current.id,
                settled_by=actor.subject.platform_user_id,
                settled_by_account_id=actor.account_id,
            )
        )
        removed = await store.delete_plan(
            session, tenant_id=actor.tenant_id, scope=scope, scope_id=scope_id
        )
        await _record(
            session,
            actor,
            platform=platform,
            channel_id=_audit_channel(scope, scope_id),
            setting=PLAN_SETTING,
            before=_plan_json(before),
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
    return removed is not None


# ---------- Catalogs ----------


def _catalog_json(row: SupportCatalogRow | None) -> JsonValue:
    if row is None:
        return None
    return {"key": row.key, "revision": row.revision, **row.spec.model_dump(mode="json")}


async def _locked_catalog(
    session: AsyncSession, actor: ChannelActor, *, key: str, expected: Expected[int | None] | None
) -> SupportCatalogRow | None:
    """The menu now, under the tenant lock; `expected` is the revision last read."""
    await lock_access_policy(session, tenant_id=actor.tenant_id)
    current = await store.get_catalog(session, tenant_id=actor.tenant_id, key=key)
    revision = None if current is None else current.revision
    _stale_check(CATALOG_SETTING, revision, expected, _catalog_json(current))
    return current


async def set_catalog(
    sessionmaker: async_sessionmaker[AsyncSession],
    actor: ChannelActor,
    *,
    platform: str,
    key: str,
    spec: SupportCatalogSpec,
    expected: Expected[int | None] | None = None,
) -> SupportCatalogRow:
    """Set or replace a menu; plans using it offer the new options on their next request."""
    _require_admin(actor)
    key = parse_catalog_key(key)
    async with sessionmaker.begin() as session:
        before = await _locked_catalog(session, actor, key=key, expected=expected)
        row = await store.set_catalog(
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
            setting=CATALOG_SETTING,
            before=_catalog_json(before),
            after=_catalog_json(row),
        )
    return row


async def remove_catalog(
    sessionmaker: async_sessionmaker[AsyncSession],
    actor: ChannelActor,
    *,
    platform: str,
    key: str,
    expected: Expected[int | None] | None = None,
) -> None:
    """Remove a menu no plan uses; an unknown key is refused."""
    _require_admin(actor)
    async with sessionmaker.begin() as session:
        before = await _locked_catalog(session, actor, key=key, expected=expected)
        if before is None:
            raise await missing_catalog(session, tenant_id=actor.tenant_id, key=key)
        if await store.plans_using(session, tenant_id=actor.tenant_id, catalog_id=before.id):
            raise ChannelChangeRefused(CATALOG_IN_USE, kind="invalid")
        await store.delete_catalog(session, tenant_id=actor.tenant_id, key=key)
        await _record(
            session,
            actor,
            platform=platform,
            channel_id=WORKSPACE_ID,
            setting=CATALOG_SETTING,
            before=_catalog_json(before),
            after=None,
        )


# ---------- Allowances ----------


async def _allowance_plan(
    session: AsyncSession, actor: ChannelActor, *, scope: PlanScope | None, scope_id: str
) -> SupportPlanRow | None:
    """The plan an allowance sits under; `scope` None is the default plan."""
    if scope is None:
        return None
    plan = await store.get_plan(session, tenant_id=actor.tenant_id, scope=scope, scope_id=scope_id)
    if plan is None:
        raise ChannelChangeRefused(NO_PLAN, kind="not_found")
    return plan


async def _change_allowance(
    sessionmaker: async_sessionmaker[AsyncSession],
    actor: ChannelActor,
    *,
    platform: str,
    scope: PlanScope | None,
    scope_id: str,
    platform_user_id: str,
    credits: Decimal | None,
    expected: Expected[Decimal | None] | None,
) -> tuple[Decimal | None, SupportAllowanceRow | None]:
    _require_admin(actor)
    platform_user_id = parse_person_id(platform, platform_user_id)
    async with sessionmaker.begin() as session:
        await lock_access_policy(session, tenant_id=actor.tenant_id)
        plan = await _allowance_plan(session, actor, scope=scope, scope_id=scope_id)
        plan_id = None if plan is None else plan.id
        current = await store.get_allowance(
            session, tenant_id=actor.tenant_id, plan_id=plan_id, platform_user_id=platform_user_id
        )
        before = None if current is None else current.credits
        _stale_check(ALLOWANCE_SETTING, before, expected, _credits_json(before))
        row = None
        if credits is None:
            await store.delete_allowance(
                session,
                tenant_id=actor.tenant_id,
                plan_id=plan_id,
                platform_user_id=platform_user_id,
            )
        else:
            row = await store.set_allowance(
                session,
                tenant_id=actor.tenant_id,
                plan_id=plan_id,
                platform_user_id=platform_user_id,
                credits=credits,
                set_by_account_id=actor.account_id,
            )
        plan_label = "default" if scope is None else f"{scope}:{scope_id}" if scope_id else scope
        await _record(
            session,
            actor,
            platform=platform,
            channel_id=_audit_channel(scope, scope_id),
            setting=ALLOWANCE_SETTING,
            before=None if before is None else {"plan": plan_label, **_credits_obj(before)},
            after=None if credits is None else {"plan": plan_label, **_credits_obj(credits)},
        )
    return before, row


def _credits_json(value: Decimal | None) -> JsonValue:
    return None if value is None else format_credits(value)


def _credits_obj(value: Decimal) -> dict[str, JsonValue]:
    return {"credits": format_credits(value)}


async def set_allowance(
    sessionmaker: async_sessionmaker[AsyncSession],
    actor: ChannelActor,
    *,
    platform: str,
    platform_user_id: str,
    credits: Decimal,
    scope: PlanScope | None = None,
    scope_id: str = "",
    expected: Expected[Decimal | None] | None = None,
) -> SupportAllowanceRow:
    """Give one person their own cap under a plan (`scope` None: the default plan)."""
    credits = parse_credits(credits)
    _, row = await _change_allowance(
        sessionmaker,
        actor,
        platform=platform,
        scope=scope,
        scope_id=scope_id,
        platform_user_id=platform_user_id,
        credits=credits,
        expected=expected,
    )
    assert row is not None
    return row


async def clear_allowance(
    sessionmaker: async_sessionmaker[AsyncSession],
    actor: ChannelActor,
    *,
    platform: str,
    platform_user_id: str,
    scope: PlanScope | None = None,
    scope_id: str = "",
    expected: Expected[Decimal | None] | None = None,
) -> bool:
    """Drop one person's own cap, back to the plan's; False when they had none."""
    before, _ = await _change_allowance(
        sessionmaker,
        actor,
        platform=platform,
        scope=scope,
        scope_id=scope_id,
        platform_user_id=platform_user_id,
        credits=None,
        expected=expected,
    )
    return before is not None


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
) -> ledger.SettleOutcome:
    """Staff settle from the support post; the adapter checked `is_support_channel` (decision 4).

    The post may sit in another tenant, so the request is found by id alone. The
    settler's id and account stay on the request (`clear_settler` wipes them on
    a purge). The audit row names an account only of the request's own tenant,
    looked up here: the one passed may belong to the tenant the post sits in.
    """
    async with sessionmaker.begin() as session:
        before = await ledger.get_request(session, escalation_id=escalation_id)
        if before is None:
            return ledger.SettleOutcome(status="not_found", row=None)
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
    "ALLOWANCE_SETTING",
    "CATALOG_IN_USE",
    "CATALOG_SETTING",
    "PLAN_SETTING",
    "SETTLE_SETTING",
    "PlanValue",
    "clear_allowance",
    "clear_plan",
    "is_support_channel",
    "may_set_support_plan",
    "missing_catalog",
    "read_plan",
    "remove_catalog",
    "set_allowance",
    "set_catalog",
    "set_plan",
    "settle",
    "settle_from_post",
    "stale_text",
]
