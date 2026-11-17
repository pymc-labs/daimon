"""Support limits for Ask a person, for platform admins acting with personal user credentials.

Limits, menus, the request channel and requests go through the audited change
service under `SET_SUPPORT`, the rule the CLI and the web follow. Credits cross
this boundary as decimal strings. Limits and menus are read per call, never cached.
"""

from __future__ import annotations

import uuid
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
from daimon.adapters.mcp.tools.slack._client import (
    _require_team_id,  # pyright: ignore[reportPrivateUsage]
)
from daimon.adapters.mcp.tools.teams._directory import locate_channel, require_client
from daimon.core.authz import Action
from daimon.core.channel_changes import ChannelActor, ChannelChangeRefused, Expected
from daimon.core.defaults.ma_index import agent_id_in_tenant
from daimon.core.errors import StaleChannelValue
from daimon.core.security_audit import record_authz_denial, record_policy_decision
from daimon.core.stores import support as store
from daimon.core.stores import support_escalation as ledger
from daimon.core.stores.support_rows import SupportEscalationRow, SupportLimitRow, SupportMenuRow
from daimon.core.support import (
    SupportError,
    SupportScope,
    format_credits,
    limit_state,
    parse_credits,
    parse_menu,
    parse_person_id,
    parse_scope,
)
from daimon.core.support_admin import (
    LimitChange,
    LimitUsage,
    expected_menu_key,
    get_limit_usage,
    list_limit_usage,
    merge_limit_change,
    options_text,
    settle_outcome_text,
    usage_text,
)
from daimon.core.support_changes import (
    ChannelPick,
    clear_limit,
    may_set_support,
    missing_menu,
    remove_menu,
    set_limit,
    set_menu,
    set_menu_use,
    set_support_channel,
    settle,
    stale_text,
)
from daimon.core.support_routing import own_channel_allowed, support_destination
from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError

_PLATFORMS = ("discord", "slack", "teams")
_ZERO = Decimal(0)


@dataclass(frozen=True)
class SupportLimitResult:
    """One limit: a place's own, or one person's at a place, with what its credits spent."""

    scope: str
    scope_id: str
    """Empty for the workspace; an agent id or a parent channel id otherwise."""
    person: str | None
    """The person's platform id for their own limit; null for the place's limit."""
    shared: str | None
    """Credits everyone at the place shares."""
    each_person: str | None
    """Credits each person may use."""
    window: str
    """'total' or 'monthly'."""
    starts_at: str | None
    ends_at: str | None
    """The end is exclusive. Dates are set by hand."""
    revision: int
    """Pass it back as `expected_revision` to change this limit."""
    shared_used: str | None
    """Settled charges plus pending holds from the shared credits; null for a person."""
    state: str
    """'active', 'not_started' or 'ended'; outside its dates, requests are refused."""
    menu: str | None
    """The key of the menu the place picked itself; null: the one above it, or the built-in one."""
    summary: str
    """The line to read back, e.g. '1.5 of 10 credits used, 2 each, each month'."""


@dataclass(frozen=True)
class SupportLimitsResult:
    limits: list[SupportLimitResult]
    """Workspace first, then agents, then channels."""
    people: list[SupportLimitResult]
    """Each person's own limits."""


@dataclass(frozen=True)
class ClearSupportLimitResult:
    scope: str
    scope_id: str
    person: str | None
    cleared: bool
    """False when there was no limit to clear."""


@dataclass(frozen=True)
class SupportMenuResult:
    key: str
    revision: int
    """Pass it back as `expected_revision` to change this menu."""
    summary: str
    """'Review (1-3 credits), Call (2 credits)'."""
    body: dict[str, object]
    """The menu as `support_menu_set` takes it."""


@dataclass(frozen=True)
class SetSupportMenuResult:
    key: str
    revision: int
    """0 when the menu was removed."""
    summary: str


@dataclass(frozen=True)
class SupportMenuUseResult:
    scope: str
    scope_id: str
    menu: str | None
    """The menu the place picked; null: the menu above it, or the built-in one."""


@dataclass(frozen=True)
class SupportChannelResult:
    own_allowed: bool
    """False when the deployment keeps its own request channel."""
    destination: str | None
    """Where requests go now; null when nowhere."""
    own: bool
    """True when `destination` is this workspace's own channel."""
    channel_id: str | None
    """The workspace's own channel, kept even while the deployment overrides it."""
    revision: int
    """Pass it back as `expected_revision`; 0 when none is set."""


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
        f"Support limits need {admin_noun(auth.platform)}, and the caller is not one. "
        "Tell them who can make the change. Do not retry."
    )


def _require_support_admin(auth: AuthIdentity) -> str:
    """The platform, once the caller may read and change support limits."""
    _require_platform_admin(auth)
    if auth.platform not in _PLATFORMS:
        raise ToolError("support limits aren't available here")
    decision = may_set_support(mcp_subject(auth, is_admin=auth.is_platform_admin))
    if not decision:
        record_authz_denial(Action.SET_SUPPORT, decision.reason)
        raise ToolError(_needs_support_admin(auth))
    record_policy_decision(Action.SET_SUPPORT, "allow")
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
) -> tuple[SupportScope, str]:
    """An agent name becomes its id; a thread becomes its parent channel."""
    try:
        place_scope, place_id = parse_scope(scope, scope_id.strip() if scope_id else None)
    except SupportError as exc:
        raise ToolError(str(exc)) from exc
    if place_scope == "agent":
        agent_id = await agent_id_in_tenant(
            runtime.client, tenant_id=auth.tenant_id, ref=place_id, records=runtime.session_factory
        )
        if agent_id is None:
            raise ToolError(f"No agent {place_id!r} here.")
        place_id = agent_id
    if place_scope == "channel":
        place_id = (await resolve_channel(runtime, auth, place_id, lenient=lenient)).channel_id
    return place_scope, place_id


def _person(platform: str, person: str | None) -> str | None:
    if person is None:
        return None
    try:
        return parse_person_id(platform, person)
    except SupportError as exc:
        raise _refused(exc) from exc


def _expected_revision(raw: int | None) -> Expected[int | None] | None:
    """0 means no row is expected."""
    return None if raw is None else Expected(raw or None)


def _limit_result(usage: LimitUsage) -> SupportLimitResult:
    return _row_result(
        usage.limit,
        shared_used=format_credits(usage.shared_used),
        state=usage.state,
        menu=usage.menu,
        summary=usage.summary,
    )


def _row_result(
    row: SupportLimitRow, *, shared_used: str | None, state: str, menu: str | None, summary: str
) -> SupportLimitResult:
    def credits(value: Decimal | None) -> str | None:
        return None if value is None else format_credits(value)

    return SupportLimitResult(
        scope=row.scope,
        scope_id=row.scope_id,
        person=row.platform_user_id,
        shared=credits(row.shared),
        each_person=credits(row.each_person),
        window=row.window,
        starts_at=row.starts_at.isoformat() if row.starts_at else None,
        ends_at=row.ends_at.isoformat() if row.ends_at else None,
        revision=row.revision,
        shared_used=shared_used,
        state=state,
        menu=menu,
        summary=summary,
    )


def _person_result(row: SupportLimitRow, *, now: datetime) -> SupportLimitResult:
    return _row_result(
        row,
        shared_used=None,
        state=limit_state(row, now=now),
        menu=None,
        summary=usage_text(row, shared_used=_ZERO, now=now),
    )


def _menu_result(row: SupportMenuRow) -> SupportMenuResult:
    return SupportMenuResult(
        key=row.key,
        revision=row.revision,
        summary=options_text(row.spec),
        body=row.spec.model_dump(mode="json", exclude_none=True),
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


async def _list_limits_impl(runtime: McpRuntime, auth: AuthIdentity) -> SupportLimitsResult:
    _require_support_admin(auth)
    now = datetime.now(UTC)
    async with runtime.session_factory() as session:
        usages = await list_limit_usage(session, tenant_id=auth.tenant_id, now=now)
        people = await store.list_limits(session, tenant_id=auth.tenant_id, people=True)
    return SupportLimitsResult(
        limits=[_limit_result(u) for u in usages],
        people=[_person_result(p, now=now) for p in people],
    )


async def _set_limit_impl(
    runtime: McpRuntime,
    auth: AuthIdentity,
    *,
    scope: str,
    scope_id: str | None,
    shared: str | None,
    each_person: str | None,
    window: str | None,
    starts_at: str | None,
    ends_at: str | None,
    person: str | None,
    expected_revision: int | None,
) -> SupportLimitResult:
    """Validate everything before the write: a refused call leaves no row behind.

    Arguments left out keep the limit's values (`merge_limit_change`), as in the CLI.
    """
    platform = _require_support_admin(auth)
    place_scope, place_id = await _place(runtime, auth, scope, scope_id)
    who = _person(platform, person)
    change = LimitChange(shared, each_person, window, starts_at, ends_at)
    try:
        async with runtime.session_factory() as session:
            spec, before = await merge_limit_change(
                session,
                tenant_id=auth.tenant_id,
                scope=place_scope,
                scope_id=place_id,
                change=change,
                platform_user_id=who,
            )
        row = await set_limit(
            runtime.session_factory,
            _actor(auth),
            platform=platform,
            spec=spec,
            expected=_expected_revision(expected_revision) or Expected(before),
        )
    except (SupportError, ChannelChangeRefused, StaleChannelValue) as exc:
        raise _refused(exc) from exc
    now = datetime.now(UTC)
    if who is not None:
        return _person_result(row, now=now)
    async with runtime.session_factory() as session:
        usage = await get_limit_usage(
            session, tenant_id=auth.tenant_id, scope=place_scope, scope_id=place_id, now=now
        )
    assert usage is not None, "the limit just set"
    return _limit_result(usage)


async def _clear_limit_impl(
    runtime: McpRuntime,
    auth: AuthIdentity,
    *,
    scope: str,
    scope_id: str | None,
    person: str | None,
    expected_revision: int | None,
) -> ClearSupportLimitResult:
    platform = _require_support_admin(auth)
    place_scope, place_id = await _place(runtime, auth, scope, scope_id, lenient=True)
    who = _person(platform, person)
    try:
        cleared = await clear_limit(
            runtime.session_factory,
            _actor(auth),
            platform=platform,
            scope=place_scope,
            scope_id=place_id,
            platform_user_id=who,
            expected=_expected_revision(expected_revision),
        )
    except (ChannelChangeRefused, StaleChannelValue) as exc:
        raise _refused(exc) from exc
    return ClearSupportLimitResult(
        scope=place_scope, scope_id=place_id, person=who, cleared=cleared
    )


async def _get_menus_impl(
    runtime: McpRuntime, auth: AuthIdentity, key: str | None
) -> list[SupportMenuResult]:
    """One menu by key, or every menu; an unknown key is refused naming the keys there are."""
    _require_support_admin(auth)
    async with runtime.session_factory() as session:
        if key is None:
            rows = await store.list_menus(session, tenant_id=auth.tenant_id)
        else:
            row = await store.get_menu(session, tenant_id=auth.tenant_id, key=key.strip())
            if row is None:
                raise ToolError(
                    str(await missing_menu(session, tenant_id=auth.tenant_id, key=key.strip()))
                )
            rows = [row]
    return [_menu_result(r) for r in rows]


async def _set_menu_impl(
    runtime: McpRuntime,
    auth: AuthIdentity,
    *,
    key: str,
    body: dict[str, object] | None,
    expected_revision: int | None,
) -> SetSupportMenuResult:
    platform = _require_support_admin(auth)
    expected = _expected_revision(expected_revision)
    try:
        if body is None:
            await remove_menu(
                runtime.session_factory, _actor(auth), platform=platform, key=key, expected=expected
            )
            return SetSupportMenuResult(key=key.strip(), revision=0, summary="")
        row = await set_menu(
            runtime.session_factory,
            _actor(auth),
            platform=platform,
            key=key,
            spec=parse_menu(body),
            expected=expected,
        )
    except (SupportError, ChannelChangeRefused, StaleChannelValue) as exc:
        raise _refused(exc) from exc
    return SetSupportMenuResult(key=row.key, revision=row.revision, summary=options_text(row.spec))


async def _use_menu_impl(
    runtime: McpRuntime,
    auth: AuthIdentity,
    *,
    key: str,
    scope: str,
    scope_id: str | None,
    expected: str | None,
) -> SupportMenuUseResult:
    """`default` drops the place's own pick; `expected` is the key last read, or `none`."""
    platform = _require_support_admin(auth)
    place_scope, place_id = await _place(runtime, auth, scope, scope_id, lenient=key == "default")
    picked = None if key == "default" else key.strip()
    try:
        expected_key = expected_menu_key(expected)
        await set_menu_use(
            runtime.session_factory,
            _actor(auth),
            platform=platform,
            scope=place_scope,
            scope_id=place_id,
            key=picked,
            expected=expected_key,
        )
    except (SupportError, ChannelChangeRefused, StaleChannelValue) as exc:
        raise _refused(exc) from exc
    return SupportMenuUseResult(scope=place_scope, scope_id=place_id, menu=picked)


async def _get_channel_impl(runtime: McpRuntime, auth: AuthIdentity) -> SupportChannelResult:
    platform = _require_support_admin(auth)
    async with runtime.session_factory() as session:
        own = await store.get_support_channel(session, tenant_id=auth.tenant_id)
        destination = await support_destination(
            session, tenant_id=auth.tenant_id, platform=platform, settings=runtime.settings
        )
    return SupportChannelResult(
        own_allowed=own_channel_allowed(runtime.settings),
        destination=None if destination is None else destination.channel_id,
        own=destination is not None and destination.own,
        channel_id=None if own is None else own.channel_id,
        revision=0 if own is None else own.revision,
    )


async def _set_channel_impl(
    runtime: McpRuntime,
    auth: AuthIdentity,
    *,
    channel_id: str | None,
    service_url: str | None,
    expected_revision: int | None,
) -> SupportChannelResult:
    """Send this workspace's requests to `channel_id`; null goes back to the default."""
    platform = _require_support_admin(auth)
    pick: ChannelPick | None = None
    if channel_id is not None:
        if platform == "teams" and not service_url:
            raise ToolError("Teams needs service_url, the bot's service URL. Nothing was saved.")
        channel = await resolve_channel(runtime, auth, channel_id)
        pick = ChannelPick(
            platform=platform,
            channel_id=channel.channel_id,
            discord_guild_id=auth.external_id if platform == "discord" else None,
            slack_team_id=_require_team_id(auth) if platform == "slack" else None,
            teams_team_id=await _teams_team(runtime, auth, channel.channel_id)
            if platform == "teams"
            else None,
            teams_service_url=service_url if platform == "teams" else None,
        )
    try:
        await set_support_channel(
            runtime.session_factory,
            _actor(auth),
            pick=pick,
            own_allowed=own_channel_allowed(runtime.settings),
            expected=_expected_revision(expected_revision),
        )
    except (ChannelChangeRefused, StaleChannelValue) as exc:
        raise _refused(exc) from exc
    return await _get_channel_impl(runtime, auth)


async def _teams_team(runtime: McpRuntime, auth: AuthIdentity, channel_id: str) -> str:
    client, _ = require_client(runtime, auth)
    return (await locate_channel(runtime, auth, client, channel_id)).team_id


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
    limit_id: uuid.UUID | None = None
    if scope is not None:
        place_scope, place_id = await _place(runtime, auth, scope, scope_id, lenient=True)
        async with runtime.session_factory() as session:
            row = await store.get_limit(
                session, tenant_id=auth.tenant_id, scope=place_scope, scope_id=place_id
            )
        if row is None:
            raise ToolError("There is no limit there.")
        limit_id = row.id
    async with runtime.session_factory() as session:
        rows = await ledger.list_requests(
            session,
            tenant_id=auth.tenant_id,
            status="pending" if pending and not undelivered else None,
            limit_id=limit_id,
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
    except (ValueError, SupportError) as exc:
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


def register_support_tools(mcp: FastMCP, runtime: McpRuntime) -> None:
    @mcp.tool(tags={"admin"})
    async def support_limit_list(ctx: Context) -> SupportLimitsResult:  # pyright: ignore[reportUnusedFunction]
        """List the support limits for Ask a person, with credits used. Admin-only.

        ``limits`` are places, workspace first, then agents, then channels; ``people``
        are each person's own limits. Read ``summary`` back. With no limit, requests
        use the default credits per person.
        """
        return await _list_limits_impl(runtime, await _auth(ctx))

    @mcp.tool(tags={"admin"})
    async def support_limit_set(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        scope: Literal["workspace", "agent", "channel"],
        scope_id: str | None = None,
        shared: str | None = None,
        each_person: str | None = None,
        window: Literal["total", "monthly"] | None = None,
        starts_at: str | None = None,
        ends_at: str | None = None,
        person: str | None = None,
        expected_revision: int | None = None,
    ) -> SupportLimitResult:
        """Set a support limit for the workspace, an agent or a channel. Admin-only.

        ``scope_id``: none for the workspace, an agent name or id, or a channel id
        (a thread id resolves to its parent). The most specific place with a limit
        governs a request; limits never stack. Arguments left out keep the limit's
        values; ``"none"`` clears ``shared``, ``each_person`` or a date. ``shared``
        is credits everyone there uses together, ``each_person`` each person may use,
        decimal strings such as ``"0.5"``. ``window``: ``total`` or
        ``monthly`` (per UTC month). ``starts_at`` and ``ends_at`` (exclusive) are
        set by hand; outside them, requests are refused. ``person`` is a
        platform user id or ``<@id>``: it sets that person's own limit at the place
        (``workspace`` means everywhere) with ``each_person`` only.
        ``expected_revision``: the ``revision`` last read, or 0 when you expect no
        limit; a mismatch saves nothing. Example: ``scope="channel"``,
        ``scope_id="222"``, ``shared="5"``, ``each_person="1"``, ``window="monthly"``.
        """
        return await _set_limit_impl(
            runtime,
            await _auth(ctx),
            scope=scope,
            scope_id=scope_id,
            shared=shared,
            each_person=each_person,
            window=window,
            starts_at=starts_at,
            ends_at=ends_at,
            person=person,
            expected_revision=expected_revision,
        )

    @mcp.tool(tags={"admin"})
    async def support_limit_clear(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        scope: Literal["workspace", "agent", "channel"],
        scope_id: str | None = None,
        person: str | None = None,
        expected_revision: int | None = None,
    ) -> ClearSupportLimitResult:
        """Remove a support limit, or one person's own limit at a place. Admin-only.

        Clearing a place's limit declines its pending requests at no charge.
        Requests there then use the limit above it, or the default credits per
        person. Example: ``scope="channel"``, ``scope_id="222"``.
        """
        return await _clear_limit_impl(
            runtime,
            await _auth(ctx),
            scope=scope,
            scope_id=scope_id,
            person=person,
            expected_revision=expected_revision,
        )

    @mcp.tool(tags={"admin"})
    async def support_menu_get(  # pyright: ignore[reportUnusedFunction]
        ctx: Context, key: str | None = None
    ) -> list[SupportMenuResult]:
        """Show one support menu by key, or every menu with no key. Admin-only.

        An unknown key is refused, naming the keys there are. Example: ``key="support"``.
        """
        return await _get_menus_impl(runtime, await _auth(ctx), key)

    @mcp.tool(tags={"admin"})
    async def support_menu_set(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        key: str,
        body: dict[str, object] | None,
        expected_revision: int | None = None,
    ) -> SetSupportMenuResult:
        """Set, replace or remove (``body`` null) a support menu. Admin-only.

        ``key``: 1 to 32 lowercase letters, digits or dashes. ``body``: a ``name`` and
        1 to 10 ``options``, each with ``key``, ``label``, an optional
        ``description``, a ``price`` and up to 4 ``fields``. Prices:
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

        ``expected_revision``: the ``revision`` last read, or 0 when you expect no
        menu; a mismatch saves nothing. A menu a place uses cannot be removed.
        """
        return await _set_menu_impl(
            runtime, await _auth(ctx), key=key, body=body, expected_revision=expected_revision
        )

    @mcp.tool(tags={"admin"})
    async def support_menu_use(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        key: str,
        scope: Literal["workspace", "agent", "channel"],
        scope_id: str | None = None,
        expected: str | None = None,
    ) -> SupportMenuUseResult:
        """Pick the support menu a place offers. Admin-only.

        ``key`` is a menu key, or ``"default"`` to drop the place's own pick: the
        menu above it, or the built-in one, applies. ``scope`` and ``scope_id`` as
        for ``support_limit_set``. ``expected``: the menu key last read for the
        place, or ``"none"``; a mismatch saves nothing. Example: ``key="support"``,
        ``scope="channel"``, ``scope_id="222"``.
        """
        return await _use_menu_impl(
            runtime, await _auth(ctx), key=key, scope=scope, scope_id=scope_id, expected=expected
        )

    @mcp.tool(tags={"admin"})
    async def support_channel_get(ctx: Context) -> SupportChannelResult:  # pyright: ignore[reportUnusedFunction]
        """Show where this workspace's support requests go. Admin-only.

        ``own_allowed`` is false when the deployment keeps its own request channel;
        then ``destination`` is that channel and ``channel_id`` is ignored.
        """
        return await _get_channel_impl(runtime, await _auth(ctx))

    @mcp.tool(tags={"admin"})
    async def support_channel_set(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        channel_id: str | None,
        service_url: str | None = None,
        expected_revision: int | None = None,
    ) -> SupportChannelResult:
        """Send this workspace's support requests to its own channel, or reset it. Admin-only.

        ``channel_id`` null goes back to the default channel. A thread id resolves to
        its parent channel. ``service_url``: the chat service URL, where the platform needs one.
        Requests already open stay where they were posted. A deployment that keeps
        its own channel refuses. ``expected_revision``: the ``revision`` of
        ``support_channel_get``, or 0 when none is set. Example: ``channel_id="222"``.
        """
        return await _set_channel_impl(
            runtime,
            await _auth(ctx),
            channel_id=channel_id,
            service_url=service_url,
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

        Pass ``scope`` and ``scope_id`` to list one place's requests. ``undelivered``
        keeps the requests no support channel received and no person handled
        (``delivered`` false): pending ones, and ones already charged at submit.
        Ones a person settled or declined are never listed, and ``pending`` is
        ignored then. Example: ``pending=False``, ``undelivered=True``.
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

        A range or quoted option takes an amount within its bounds. A request
        already charged at submit (``charge`` shown, not pending) keeps that
        charge when ``charge`` equals it, or is refunded when null. To correct
        or refund one a person settled, pass its current charge as
        ``expected_charge``. The person who asked is not told; read ``text`` back.
        Example: ``request_id`` from ``support_requests_list``, ``charge="2.5"``.
        """
        return await _settle_request_impl(
            runtime,
            await _auth(ctx),
            request_id=request_id,
            charge=charge,
            expected_charge=expected_charge,
        )
