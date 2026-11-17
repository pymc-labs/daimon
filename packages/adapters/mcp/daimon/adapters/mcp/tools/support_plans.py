"""Support plans for Ask a person, for platform admins acting with personal user credentials.

Plans, menus and requests go through the audited change service under
`SET_SUPPORT_PLAN`, the rule the CLI and the web follow. Credits cross this
boundary as decimal strings. Plans and menus are read per call, never cached.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal, cast

from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools._authz_facts import mcp_subject
from daimon.adapters.mcp.tools._channel_target import resolve_channel
from daimon.adapters.mcp.tools._ctx import (
    _auth,  # pyright: ignore[reportPrivateUsage]
    _require_platform_admin,  # pyright: ignore[reportPrivateUsage]
)
from daimon.adapters.mcp.tools._words import admin_noun
from daimon.core.authz import Action
from daimon.core.channel_changes import ChannelActor, ChannelChangeRefused, Expected
from daimon.core.defaults.ma_index import find_agent_by_daimon_tag
from daimon.core.errors import StaleChannelValue
from daimon.core.security_audit import record_authz_denial, record_policy_decision
from daimon.core.stores import support_escalation as ledger
from daimon.core.stores import support_plans as store
from daimon.core.stores.support_rows import SupportCatalogRow, SupportEscalationRow
from daimon.core.support_plan_admin import (
    PlanChange,
    PlanUsage,
    get_plan_usage,
    list_plan_usage,
    merge_plan_change,
    options_text,
    parse_plan_value,
    settle_outcome_text,
)
from daimon.core.support_plan_changes import (
    PlanValue,
    clear_allowance,
    clear_plan,
    may_set_support_plan,
    remove_catalog,
    set_allowance,
    set_catalog,
    set_plan,
    settle,
    stale_text,
)
from daimon.core.support_plans import (
    PlanScope,
    SupportPlanError,
    format_credits,
    parse_catalog,
    parse_credits,
    parse_person_id,
    parse_scope,
)
from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError

_PLATFORMS = ("discord", "slack", "teams")


@dataclass(frozen=True)
class SupportPlanResult:
    """One plan with what its pool spent in the window holding now."""

    scope: str
    scope_id: str
    """Empty for the workspace plan; an agent id or a parent channel id otherwise."""
    catalog: str | None
    """The menu key; null is the default menu."""
    pool: str | None
    per_person: str | None
    window: str
    starts_at: str | None
    ends_at: str | None
    pool_used: str
    """Settled charges plus pending holds in the window."""
    state: str
    """'active', 'not_started' or 'ended'; only an active plan takes requests."""
    summary: str
    """The line to read back, e.g. '1.5 of 10 credits used, 2 per person, this month'."""
    allowances: dict[str, str]
    """Platform user id to that person's own cap."""


@dataclass(frozen=True)
class ClearSupportPlanResult:
    scope: str
    scope_id: str
    cleared: bool
    """False when there was no plan to clear."""


@dataclass(frozen=True)
class SupportAllowanceResult:
    platform_user_id: str
    scope: str | None
    """Null for the default allowance, where no plan applies."""
    scope_id: str
    credits: str | None
    """The person's own cap; null once cleared."""


@dataclass(frozen=True)
class SupportCatalogResult:
    key: str
    revision: int
    """Pass it back as `expected_revision` to change this menu."""
    summary: str
    """'Review (1-3 credits), Call (2 credits)'."""
    catalog: dict[str, object]
    """The menu as `support_catalog_set` takes it."""


@dataclass(frozen=True)
class SetSupportCatalogResult:
    key: str
    revision: int
    """0 when the menu was removed."""
    summary: str


@dataclass(frozen=True)
class SupportRequestResult:
    id: str
    created_at: str
    platform_user_id: str
    channel_id: str
    option: str | None
    status: str
    """'pending', 'settled' or 'declined'."""
    counted: str
    """What it spends: its charge once settled, its hold while pending."""
    charge: str | None
    charged_to: str
    delivered: bool
    """False when no support channel received it: nobody on the team has seen it."""


@dataclass(frozen=True)
class SettleSupportRequestResult:
    id: str
    status: str
    charge: str | None
    text: str
    """Read it back; the person who asked is not told."""


def _needs_support_admin(auth: AuthIdentity) -> str:
    return (
        f"Support plans need {admin_noun(auth.platform)}, and the caller is not one. "
        "Tell them who can make the change. Do not retry."
    )


def _require_support_admin(auth: AuthIdentity) -> str:
    """The platform, once the caller may read and change support plans."""
    _require_platform_admin(auth)
    if auth.platform not in _PLATFORMS:
        raise ToolError("support plans aren't available here")
    decision = may_set_support_plan(mcp_subject(auth, is_admin=auth.is_platform_admin))
    if not decision:
        record_authz_denial(Action.SET_SUPPORT_PLAN, decision.reason)
        raise ToolError(_needs_support_admin(auth))
    record_policy_decision(Action.SET_SUPPORT_PLAN, "allow")
    return cast(str, auth.platform)


def _actor(auth: AuthIdentity) -> ChannelActor:
    return ChannelActor(
        tenant_id=auth.tenant_id,
        account_id=auth.account_id,
        subject=mcp_subject(auth, is_admin=auth.is_platform_admin),
        surface="chat",
    )


def _refused(exc: Exception) -> ToolError:
    if isinstance(exc, StaleChannelValue):
        return ToolError(f"{stale_text(exc)} Nothing was saved.")
    return ToolError(f"{exc} Nothing was saved.")


async def _place(
    runtime: McpRuntime,
    auth: AuthIdentity,
    scope: str,
    scope_id: str | None,
    *,
    lenient: bool = False,
) -> tuple[PlanScope, str]:
    """An agent name becomes its id; a thread becomes its parent channel."""
    try:
        plan_scope, place_id = parse_scope(scope, scope_id.strip() if scope_id else None)
    except SupportPlanError as exc:
        raise ToolError(str(exc)) from exc
    if plan_scope == "agent" and not place_id.startswith("agent_"):
        agent = await find_agent_by_daimon_tag(
            runtime.client, tenant_id=auth.tenant_id, name=place_id, records=runtime.session_factory
        )
        if agent is None:
            raise ToolError(f"No agent named {place_id!r} here.")
        place_id = agent.id
    if plan_scope == "channel":
        place_id = (await resolve_channel(runtime, auth, place_id, lenient=lenient)).channel_id
    return plan_scope, place_id


def _expected_plan(
    raw: Mapping[str, object] | Literal["none"] | None,
) -> Expected[PlanValue | None] | None:
    if raw is None:
        return None
    if raw == "none":
        return Expected(None)
    try:
        return Expected(parse_plan_value(raw))
    except SupportPlanError as exc:
        raise ToolError(f"expected: {exc}") from exc


def _expected_revision(raw: int | None) -> Expected[int | None] | None:
    return None if raw is None else Expected(raw or None)


def _plan_result(usage: PlanUsage) -> SupportPlanResult:
    value = usage.value.as_json()
    return SupportPlanResult(
        scope=usage.plan.scope,
        scope_id=usage.plan.scope_id,
        catalog=usage.value.catalog,
        pool=cast("str | None", value["pool"]),
        per_person=cast("str | None", value["per_person"]),
        window=usage.plan.window,
        starts_at=cast("str | None", value["starts_at"]),
        ends_at=cast("str | None", value["ends_at"]),
        pool_used=format_credits(usage.pool_used),
        state=usage.state,
        summary=usage.summary,
        allowances={a.platform_user_id: format_credits(a.credits) for a in usage.allowances},
    )


def _catalog_result(row: SupportCatalogRow) -> SupportCatalogResult:
    return SupportCatalogResult(
        key=row.key,
        revision=row.revision,
        summary=options_text(row.spec),
        catalog=row.spec.model_dump(mode="json", exclude_none=True),
    )


def _request_result(row: SupportEscalationRow) -> SupportRequestResult:
    return SupportRequestResult(
        id=str(row.id),
        created_at=row.created_at.isoformat(),
        platform_user_id=row.platform_user_id,
        channel_id=row.channel_id,
        option=row.option.label if row.option else None,
        status=row.status,
        counted=format_credits(row.counted),
        charge=None if row.charge is None else format_credits(row.charge),
        charged_to=row.charged_to,
        delivered=row.delivered_at is not None,
    )


async def _list_plans_impl(runtime: McpRuntime, auth: AuthIdentity) -> list[SupportPlanResult]:
    _require_support_admin(auth)
    async with runtime.session_factory() as session:
        plans = await list_plan_usage(session, tenant_id=auth.tenant_id, now=datetime.now(UTC))
    return [_plan_result(p) for p in plans]


async def _set_plan_impl(
    runtime: McpRuntime,
    auth: AuthIdentity,
    *,
    scope: str,
    scope_id: str | None,
    catalog: str | None,
    pool: str | None,
    per_person: str | None,
    window: str | None,
    starts_at: str | None,
    ends_at: str | None,
    expected: dict[str, object] | Literal["none"] | None,
) -> SupportPlanResult:
    """Validate everything before the write: a refused call leaves no row behind.

    Arguments left out keep the plan's values (`merge_plan`), as in the CLI.
    """
    platform = _require_support_admin(auth)
    plan_scope, place_id = await _place(runtime, auth, scope, scope_id)
    change = PlanChange(catalog, pool, per_person, window, starts_at, ends_at)
    try:
        async with runtime.session_factory() as session:
            spec, before = await merge_plan_change(
                session,
                tenant_id=auth.tenant_id,
                scope=plan_scope,
                scope_id=place_id,
                change=change,
            )
        await set_plan(
            runtime.session_factory,
            _actor(auth),
            platform=platform,
            spec=spec,
            expected=_expected_plan(expected) or Expected(before),
        )
    except (SupportPlanError, ChannelChangeRefused, StaleChannelValue) as exc:
        raise _refused(exc) from exc
    async with runtime.session_factory() as session:
        usage = await get_plan_usage(
            session,
            tenant_id=auth.tenant_id,
            scope=plan_scope,
            scope_id=place_id,
            now=datetime.now(UTC),
        )
    assert usage is not None, "the plan just set"
    return _plan_result(usage)


async def _clear_plan_impl(
    runtime: McpRuntime,
    auth: AuthIdentity,
    *,
    scope: str,
    scope_id: str | None,
    expected: dict[str, object] | Literal["none"] | None,
) -> ClearSupportPlanResult:
    platform = _require_support_admin(auth)
    plan_scope, place_id = await _place(runtime, auth, scope, scope_id, lenient=True)
    try:
        cleared = await clear_plan(
            runtime.session_factory,
            _actor(auth),
            platform=platform,
            scope=plan_scope,
            scope_id=place_id,
            expected=_expected_plan(expected),
        )
    except (ChannelChangeRefused, StaleChannelValue) as exc:
        raise _refused(exc) from exc
    return ClearSupportPlanResult(scope=plan_scope, scope_id=place_id, cleared=cleared)


async def _set_allowance_impl(
    runtime: McpRuntime,
    auth: AuthIdentity,
    *,
    platform_user_id: str,
    credits: str | None,
    scope: str | None,
    scope_id: str | None,
    expected: str | None,
) -> SupportAllowanceResult:
    platform = _require_support_admin(auth)
    try:
        user_id = parse_person_id(platform, platform_user_id)
    except SupportPlanError as exc:
        raise _refused(exc) from exc
    plan_scope: PlanScope | None = None
    place_id = ""
    if scope is not None:
        plan_scope, place_id = await _place(runtime, auth, scope, scope_id, lenient=credits is None)
    try:
        last: Expected[Decimal | None] | None = None
        if expected is not None:
            last = Expected(
                None if expected == "none" else parse_credits(expected, name="expected")
            )
        place = {"platform": platform, "platform_user_id": user_id}
        if credits is None:
            await clear_allowance(
                runtime.session_factory,
                _actor(auth),
                **place,
                scope=plan_scope,
                scope_id=place_id,
                expected=last,
            )
            cap = None
        else:
            row = await set_allowance(
                runtime.session_factory,
                _actor(auth),
                **place,
                credits=parse_credits(credits, name="credits"),
                scope=plan_scope,
                scope_id=place_id,
                expected=last,
            )
            cap = format_credits(row.credits)
    except (SupportPlanError, ChannelChangeRefused, StaleChannelValue) as exc:
        raise _refused(exc) from exc
    return SupportAllowanceResult(
        platform_user_id=user_id, scope=plan_scope, scope_id=place_id, credits=cap
    )


async def _get_catalogs_impl(
    runtime: McpRuntime, auth: AuthIdentity, key: str | None
) -> list[SupportCatalogResult]:
    _require_support_admin(auth)
    async with runtime.session_factory() as session:
        if key is None:
            rows = await store.list_catalogs(session, tenant_id=auth.tenant_id)
        else:
            row = await store.get_catalog(session, tenant_id=auth.tenant_id, key=key.strip())
            rows = [] if row is None else [row]
    return [_catalog_result(r) for r in rows]


async def _set_catalog_impl(
    runtime: McpRuntime,
    auth: AuthIdentity,
    *,
    key: str,
    catalog: dict[str, object] | None,
    expected_revision: int | None,
) -> SetSupportCatalogResult:
    platform = _require_support_admin(auth)
    expected = _expected_revision(expected_revision)
    try:
        if catalog is None:
            await remove_catalog(
                runtime.session_factory, _actor(auth), platform=platform, key=key, expected=expected
            )
            return SetSupportCatalogResult(key=key, revision=0, summary="")
        row = await set_catalog(
            runtime.session_factory,
            _actor(auth),
            platform=platform,
            key=key,
            spec=parse_catalog(catalog),
            expected=expected,
        )
    except (SupportPlanError, ChannelChangeRefused, StaleChannelValue) as exc:
        raise _refused(exc) from exc
    return SetSupportCatalogResult(
        key=row.key, revision=row.revision, summary=options_text(row.spec)
    )


async def _list_requests_impl(
    runtime: McpRuntime,
    auth: AuthIdentity,
    *,
    pending: bool,
    scope: str | None,
    scope_id: str | None,
    limit: int,
    undelivered: bool = False,
) -> list[SupportRequestResult]:
    _require_support_admin(auth)
    plan_id: uuid.UUID | None = None
    if scope is not None:
        plan_scope, place_id = await _place(runtime, auth, scope, scope_id, lenient=True)
        async with runtime.session_factory() as session:
            plan = await store.get_plan(
                session, tenant_id=auth.tenant_id, scope=plan_scope, scope_id=place_id
            )
        if plan is None:
            raise ToolError("There is no plan there.")
        plan_id = plan.id
    async with runtime.session_factory() as session:
        rows = await ledger.list_requests(
            session,
            tenant_id=auth.tenant_id,
            status="pending" if pending else None,
            plan_id=plan_id,
            undelivered=undelivered,
            limit=max(1, min(limit, 200)),
        )
    return [_request_result(r) for r in rows]


async def _settle_request_impl(
    runtime: McpRuntime,
    auth: AuthIdentity,
    *,
    request_id: str,
    charge: str | None,
    expected_charge: str | None,
) -> SettleSupportRequestResult:
    _require_support_admin(auth)
    try:
        escalation_id = uuid.UUID(request_id)
        amount = None if charge is None else parse_credits(charge, name="charge")
        correction: Decimal | None = None
        if expected_charge is not None:
            correction = parse_credits(expected_charge, name="expected_charge")
    except (ValueError, SupportPlanError) as exc:
        raise ToolError(f"{exc}. Nothing was saved.") from exc
    try:
        outcome = await settle(
            runtime.session_factory,
            _actor(auth),
            escalation_id=escalation_id,
            charge=amount,
            default_allowance=runtime.settings.support.credits_per_user,
            expected=None if correction is None else Expected(correction),
        )
    except (ChannelChangeRefused, StaleChannelValue) as exc:
        raise _refused(exc) from exc
    text = settle_outcome_text(outcome, correction=correction is not None)
    if outcome.status not in ("settled", "declined") or outcome.row is None:
        raise ToolError(text)
    row = outcome.row
    return SettleSupportRequestResult(
        id=str(row.id),
        status=row.status,
        charge=None if row.charge is None else format_credits(row.charge),
        text=text,
    )


def register_support_plan_tools(mcp: FastMCP, runtime: McpRuntime) -> None:
    @mcp.tool(tags={"admin"})
    async def support_plan_list(ctx: Context) -> list[SupportPlanResult]:  # pyright: ignore[reportUnusedFunction]
        """List support plans for Ask a person, with credits used. Admin-only.

        Workspace first, then agents, then channels. Read ``summary`` back.
        With no plan, requests use the default allowance per person.
        """
        return await _list_plans_impl(runtime, await _auth(ctx))

    @mcp.tool(tags={"admin"})
    async def support_plan_set(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        scope: Literal["workspace", "agent", "channel"],
        scope_id: str | None = None,
        catalog: str | None = None,
        pool: str | None = None,
        per_person: str | None = None,
        window: Literal["total", "monthly", "fixed"] | None = None,
        starts_at: str | None = None,
        ends_at: str | None = None,
        expected: dict[str, object] | Literal["none"] | None = None,
    ) -> SupportPlanResult:
        """Set the support plan of the workspace, an agent or a channel. Admin-only.

        ``scope_id``: none for the workspace, an agent name or id, or a channel id
        (a thread id resolves to its parent). The most specific plan governs a
        request; plans never stack. Arguments left out keep the plan's values;
        ``"none"`` clears ``pool``, ``per_person`` or a date. ``pool`` is credits
        shared by everyone, ``per_person`` each person's cap, decimal strings such
        as ``"0.5"``. ``catalog``: a menu key or ``"default"``. ``window``: ``total``
        since ``starts_at``, ``monthly`` per UTC month, ``fixed`` from ``starts_at``
        until ``ends_at`` (exclusive), set by hand. A plan outside its dates refuses
        requests. ``expected``: the plan as ``support_plan_list`` gave it, or ``"none"``;
        a mismatch saves nothing.
        """
        return await _set_plan_impl(
            runtime,
            await _auth(ctx),
            scope=scope,
            scope_id=scope_id,
            catalog=catalog,
            pool=pool,
            per_person=per_person,
            window=window,
            starts_at=starts_at,
            ends_at=ends_at,
            expected=expected,
        )

    @mcp.tool(tags={"admin"})
    async def support_plan_clear(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        scope: Literal["workspace", "agent", "channel"],
        scope_id: str | None = None,
        expected: dict[str, object] | Literal["none"] | None = None,
    ) -> ClearSupportPlanResult:
        """Remove a support plan and the own caps under it. Admin-only.

        Requests there fall back to the next plan up, or the default allowance.
        """
        return await _clear_plan_impl(
            runtime, await _auth(ctx), scope=scope, scope_id=scope_id, expected=expected
        )

    @mcp.tool(tags={"admin"})
    async def support_allowance_set(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        platform_user_id: str,
        credits: str | None,
        scope: Literal["workspace", "agent", "channel"] | None = None,
        scope_id: str | None = None,
        expected: str | None = None,
    ) -> SupportAllowanceResult:
        """Give one person their own cap under a plan, or clear it (``credits`` null). Admin-only.

        ``platform_user_id`` is the person's id on the platform, or ``<@id>``.
        No ``scope`` is the default allowance, where no plan applies; otherwise
        the plan's place as for ``support_plan_set``. ``credits`` is a decimal
        string. ``expected``: the cap last read, or ``"none"``; a mismatch saves nothing.
        """
        return await _set_allowance_impl(
            runtime,
            await _auth(ctx),
            platform_user_id=platform_user_id,
            credits=credits,
            scope=scope,
            scope_id=scope_id,
            expected=expected,
        )

    @mcp.tool(tags={"admin"})
    async def support_catalog_get(  # pyright: ignore[reportUnusedFunction]
        ctx: Context, key: str | None = None
    ) -> list[SupportCatalogResult]:
        """Show one support menu by key, or every menu with no key. Admin-only."""
        return await _get_catalogs_impl(runtime, await _auth(ctx), key)

    @mcp.tool(tags={"admin"})
    async def support_catalog_set(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        key: str,
        catalog: dict[str, object] | None,
        expected_revision: int | None = None,
    ) -> SetSupportCatalogResult:
        """Set, replace or remove (``catalog`` null) a support menu. Admin-only.

        ``catalog``: a ``name`` and 1 to 10 ``options``, each with ``key``, ``label``,
        an optional ``description``, a ``price`` and up to 4 ``fields``. Prices:
        ``{"kind": "fixed", "amount"}``; ``{"kind": "steps", "label", "steps":
        [{"label", "amount"}]}``; ``{"kind": "range", "min", "max"}``; ``{"kind":
        "quoted"}`` with optional ``min`` and ``max``. Amounts are decimal strings.
        A field has ``key``, ``label`` and ``kind``: ``text`` (``max_chars`` up to
        200), ``paragraph`` (up to 3000) or ``choice`` (``choices``). Example::

            {"name": "Support options", "options": [
              {"key": "review_thread", "label": "Review this thread",
               "price": {"kind": "fixed", "amount": "0.5"}},
              {"key": "questions", "label": "Ask multiple questions",
               "price": {"kind": "fixed", "amount": "1"},
               "fields": [{"key": "questions", "kind": "paragraph",
                           "label": "Your questions", "max_chars": 2000}]},
              {"key": "coaching", "label": "Live coaching (1 hour)",
               "price": {"kind": "fixed", "amount": "1"},
               "fields": [{"key": "slots", "kind": "paragraph",
                           "label": "Times that suit you (with time zone)",
                           "max_chars": 500}]},
              {"key": "hands_on", "label": "Hands-on support (1 hour)",
               "price": {"kind": "fixed", "amount": "1"},
               "fields": [{"key": "task", "kind": "paragraph",
                           "label": "What should we do?", "max_chars": 2000}]}]}

        ``expected_revision``: the revision last read, or 0 when you expect none;
        a mismatch saves nothing. A menu a plan uses cannot be removed.
        """
        return await _set_catalog_impl(
            runtime,
            await _auth(ctx),
            key=key,
            catalog=catalog,
            expected_revision=expected_revision,
        )

    @mcp.tool(tags={"admin"})
    async def support_requests_list(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        pending: bool = True,
        scope: Literal["workspace", "agent", "channel"] | None = None,
        scope_id: str | None = None,
        limit: int = 50,
        undelivered: bool = False,
    ) -> list[SupportRequestResult]:
        """List support requests, newest first; pending only by default. Admin-only.

        Pass ``scope`` and ``scope_id`` to list one plan's requests. ``undelivered``
        keeps the requests no support channel received (``delivered`` false); pass
        ``pending=False`` with it to include settled ones.
        """
        return await _list_requests_impl(
            runtime,
            await _auth(ctx),
            pending=pending,
            scope=scope,
            scope_id=scope_id,
            limit=limit,
            undelivered=undelivered,
        )

    @mcp.tool(tags={"admin"})
    async def support_requests_settle(  # pyright: ignore[reportUnusedFunction]
        ctx: Context, request_id: str, charge: str | None, expected_charge: str | None = None
    ) -> SettleSupportRequestResult:
        """Settle a support request at ``charge`` credits, or decline it (null). Admin-only.

        A range or quoted option takes an amount within its bounds. To correct
        or refund a settled request, pass its current charge as
        ``expected_charge``. The person who asked is not told; read ``text`` back.
        """
        return await _settle_request_impl(
            runtime,
            await _auth(ctx),
            request_id=request_id,
            charge=charge,
            expected_charge=expected_charge,
        )
