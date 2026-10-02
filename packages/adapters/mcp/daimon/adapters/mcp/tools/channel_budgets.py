"""Channel budget tools: read a channel's spending budget; admins set, clear and list them.

A budget caps what one channel may spend (see ``daimon.core.channel_budget``
for how spend and windows are counted). Reading one is a member action for a
channel the caller can see. Listing, setting and clearing act on the whole
server or workspace, so they are admin-tagged and re-checked in the impl.
Operator tokens read them with ``tenant:read`` and change them with
``channels:write``. Money crosses this boundary as decimal strings, both ways.
"""

from __future__ import annotations

import contextlib
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, cast

from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools._ctx import (
    _auth,  # pyright: ignore[reportPrivateUsage]
    _require_admin,  # pyright: ignore[reportPrivateUsage]
)
from daimon.adapters.mcp.tools._scopes import require_scope, scope_tags
from daimon.adapters.mcp.tools.discord import resolve_visible_channel
from daimon.adapters.mcp.tools.setup_target import require_turn_origin
from daimon.adapters.mcp.tools.slack._client import (
    _require_slack_identity,  # pyright: ignore[reportPrivateUsage]
    _require_team_id,  # pyright: ignore[reportPrivateUsage]
    slack_web_client,
)
from daimon.adapters.mcp.tools.slack._visibility import check_channel_access
from daimon.core.channel_budget import (
    ChannelBudgetError,
    ChannelBudgetStatus,
    describe_budget,
    get_channel_budget_status,
    load_budget_status,
    parse_budget_spec,
)
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.stores import channel_budgets as store
from daimon.core.stores.direct_messages import get_source_channel
from daimon.core.stores.turn_origins import get_active_origin
from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from slack_sdk.errors import SlackApiError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_PLATFORMS = ("discord", "slack")
_NEEDS_CHANNEL = (
    "channel_id is required: pass this turn's origin_context_id, the id from "
    '<channel role="parent_channel">, or the channel the user named.'
)


@dataclass(frozen=True)
class ChannelBudgetResult:
    """One channel's budget and what it has spent in the current window."""

    channel_id: str
    limit_usd: str
    window: str
    """'monthly' (UTC calendar month), 'total' (since starts_at, or ever) or 'fixed'."""
    starts_at: str | None
    ends_at: str | None
    spent_usd: str
    """Debited spend in the window, markup included."""
    remaining_usd: str
    active: bool
    """False before a total or fixed window's start, or after a fixed one ends: it gates nothing."""
    summary: str
    """The line to read back, e.g. '$1.20 of $5.00 (monthly)'."""


@dataclass(frozen=True)
class ChannelBudgetLookup:
    """Result of get_channel_budget."""

    channel_id: str
    """The channel the budget applies to: a thread resolves to its parent."""
    budget: ChannelBudgetResult | None
    """None when the channel has no budget; only the balance and per-person caps apply."""


@dataclass(frozen=True)
class ClearChannelBudgetResult:
    channel_id: str
    cleared: bool
    """False when the channel had no budget to clear."""


def _result(status: ChannelBudgetStatus) -> ChannelBudgetResult:
    budget = status.budget
    return ChannelBudgetResult(
        channel_id=budget.channel_id,
        limit_usd=str(budget.limit_usd),
        window=budget.window,
        starts_at=budget.starts_at.isoformat() if budget.starts_at else None,
        ends_at=budget.ends_at.isoformat() if budget.ends_at else None,
        spent_usd=str(status.spent_usd),
        remaining_usd=str(status.remaining_usd),
        active=status.is_active,
        summary=describe_budget(status),
    )


def _require_platform(auth: AuthIdentity) -> str:
    if auth.platform not in _PLATFORMS:
        raise ToolError("channel budgets exist only for Discord servers and Slack workspaces")
    return cast(str, auth.platform)


def _require_channel_id(channel_id: str | None) -> str:
    if channel_id is None or not channel_id.strip():
        raise ToolError(
            'channel_id is required: the id from <channel role="parent_channel"> '
            "or the channel the user named."
        )
    return channel_id.strip()


async def _resolve_slack_channel(runtime: McpRuntime, auth: AuthIdentity, channel_id: str) -> str:
    """A Slack channel the caller can see; a `<channel>:<thread ts>` id resolves to the channel."""
    channel_id = channel_id.partition(":")[0]
    client = await slack_web_client(runtime, team_id=_require_team_id(auth))
    try:
        info = await client.conversations_info(channel=channel_id)  # pyright: ignore[reportUnknownMemberType]
    except SlackApiError as err:
        raise ToolError(f"Slack could not find {channel_id} in this workspace") from err
    channel = cast("dict[str, object]", info["channel"])
    await check_channel_access(client, channel=channel, user_id=_require_slack_identity(auth))
    return channel_id


async def _budget_channel(runtime: McpRuntime, auth: AuthIdentity, channel_id: str) -> str:
    """The channel id a budget is stored under, after confirming the caller can see it."""
    if _require_platform(auth) == "discord":
        if not channel_id.isdigit():
            raise ToolError(f"{channel_id!r} is not a Discord channel id")
        return await resolve_visible_channel(runtime, auth, channel_id)
    return await _resolve_slack_channel(runtime, auth, channel_id)


def _is_dm_scope(thread_id: str) -> bool:
    return thread_id.startswith("dm:")


async def origin_budget_channel(
    sessionmaker: async_sessionmaker[AsyncSession],
    auth: AuthIdentity,
    origin_context_id: str | None,
) -> str | None:
    """The channel a tool call's spend counts against: its turn's parent channel.

    A DM counts toward the channel it was started from. None without a live
    origin of this caller and responder, or in an older DM, so the call is
    simply not attributed; tools that need a channel use `require_turn_origin`
    instead, which explains the refusal.
    """
    if not origin_context_id or auth.platform not in _PLATFORMS:
        return None
    try:
        origin_id = uuid.UUID(origin_context_id)
    except ValueError:
        return None
    async with sessionmaker() as session:
        origin = await get_active_origin(
            session,
            origin_id=origin_id,
            tenant_id=auth.tenant_id,
            account_id=auth.account_id,
            platform=cast(str, auth.platform),
            now=datetime.now(UTC),
        )
        if origin is None or (
            auth.agent_id is not None
            and auth.agent_id
            != derive_agent_uuid(tenant_id=auth.tenant_id, ma_agent_id=origin.responder_ma_agent_id)
        ):
            return None
        if _is_dm_scope(origin.thread_id):
            return await get_source_channel(
                session, tenant_id=auth.tenant_id, scope_id=origin.thread_id
            )
    return origin.parent_channel_id


async def _origin_channel(
    runtime: McpRuntime, auth: AuthIdentity, origin_context_id: str | None
) -> str:
    """The calling turn's parent channel, or its DM's source; the origin proves access."""
    if not origin_context_id:
        raise ToolError(_NEEDS_CHANNEL)
    origin = await require_turn_origin(runtime, auth, origin_context_id)
    if not _is_dm_scope(origin.thread_id):
        return origin.parent_channel_id
    async with runtime.session_factory() as session:
        source = await get_source_channel(
            session, tenant_id=auth.tenant_id, scope_id=origin.thread_id
        )
    if source is None:
        raise ToolError("a direct message belongs to no channel, so no channel budget applies")
    return source


async def _get_channel_budget_impl(
    runtime: McpRuntime,
    auth: AuthIdentity,
    channel_id: str | None,
    origin_context_id: str | None = None,
) -> ChannelBudgetLookup:
    require_scope(auth, "tenant:read")
    platform = _require_platform(auth)
    if channel_id is not None and channel_id.strip():
        target = await _budget_channel(runtime, auth, channel_id.strip())
    else:
        target = await _origin_channel(runtime, auth, origin_context_id)
    async with runtime.session_factory() as session:
        status = await get_channel_budget_status(
            session,
            tenant_id=auth.tenant_id,
            platform=platform,
            channel_id=target,
            now=datetime.now(UTC),
        )
    return ChannelBudgetLookup(
        channel_id=target, budget=_result(status) if status is not None else None
    )


async def _list_channel_budgets_impl(
    runtime: McpRuntime, auth: AuthIdentity
) -> list[ChannelBudgetResult]:
    require_scope(auth, "tenant:read")
    _require_admin(auth)
    platform = _require_platform(auth)
    now = datetime.now(UTC)
    async with runtime.session_factory() as session:
        budgets = await store.list_channel_budgets(session, tenant_id=auth.tenant_id)
        return [
            _result(await load_budget_status(session, budget, now=now))
            for budget in budgets
            if budget.platform == platform
        ]


async def _set_channel_budget_impl(
    runtime: McpRuntime,
    auth: AuthIdentity,
    *,
    channel_id: str,
    limit_usd: str,
    window: str,
    starts_at: str | None,
    ends_at: str | None,
) -> ChannelBudgetResult:
    """Validate everything before the write: a refused call leaves no row behind."""
    require_scope(auth, "channels:write")
    _require_admin(auth)
    platform = _require_platform(auth)
    try:
        spec = parse_budget_spec(
            limit_usd=limit_usd, window=window, starts_at=starts_at, ends_at=ends_at
        )
    except ChannelBudgetError as err:
        raise ToolError(f"{err}. Nothing was saved.") from err
    target = await _budget_channel(runtime, auth, _require_channel_id(channel_id))
    async with runtime.session_factory.begin() as session:
        budget = await store.set_channel_budget(
            session,
            tenant_id=auth.tenant_id,
            platform=platform,
            channel_id=target,
            limit_usd=spec.limit_usd,
            window=spec.window,
            starts_at=spec.starts_at,
            ends_at=spec.ends_at,
            set_by_account_id=auth.account_id,
        )
        return _result(await load_budget_status(session, budget, now=datetime.now(UTC)))


async def _clear_channel_budget_impl(
    runtime: McpRuntime, auth: AuthIdentity, channel_id: str
) -> ClearChannelBudgetResult:
    """Clear a channel's budget; a visible Discord thread clears its parent's.

    Any other id is used as given, so a budget on a channel since deleted or
    hidden can still be cleared.
    """
    require_scope(auth, "channels:write")
    _require_admin(auth)
    platform = _require_platform(auth)
    target = _require_channel_id(channel_id).partition(":")[0]
    if platform == "discord" and target.isdigit():
        with contextlib.suppress(ToolError):
            target = await _budget_channel(runtime, auth, target)
    async with runtime.session_factory.begin() as session:
        cleared = await store.delete_channel_budget(
            session, tenant_id=auth.tenant_id, platform=platform, channel_id=target
        )
    return ClearChannelBudgetResult(channel_id=target, cleared=cleared)


def register_channel_budget_tools(mcp: FastMCP, runtime: McpRuntime) -> None:
    @mcp.tool(tags={"discord", "slack", *scope_tags("tenant:read")})
    async def get_channel_budget(  # pyright: ignore[reportUnusedFunction]
        ctx: Context, channel_id: str | None = None, origin_context_id: str | None = None
    ) -> ChannelBudgetLookup:
        """Show a channel's spending budget: its limit, window and what it has spent.

        For the channel this turn is in (in a DM, the channel it was moved
        from), omit ``channel_id`` and pass this turn's
        ``origin_context_id``. For another channel, pass its id; a
        thread id resolves to its parent channel. Any member may read the
        budget of a channel they can see. ``budget`` is null when the
        channel has none. Read ``summary`` back to the user.
        """
        return await _get_channel_budget_impl(
            runtime, await _auth(ctx), channel_id, origin_context_id
        )

    @mcp.tool(tags={"admin", *scope_tags("tenant:read")})
    async def list_channel_budgets(ctx: Context) -> list[ChannelBudgetResult]:  # pyright: ignore[reportUnusedFunction]
        """List every channel budget in this server or workspace with its spend. Admin-only."""
        return await _list_channel_budgets_impl(runtime, await _auth(ctx))

    @mcp.tool(tags={"admin", *scope_tags("channels:write")})
    async def set_channel_budget(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        channel_id: str,
        limit_usd: str,
        window: Literal["monthly", "total", "fixed"],
        starts_at: str | None = None,
        ends_at: str | None = None,
    ) -> ChannelBudgetResult:
        """Set or replace a channel's spending budget. Admin-only.

        Once the channel's spend in the window reaches ``limit_usd`` (a
        decimal string such as ``"25.00"``; ``"0"`` stops the channel), new
        turns there are refused, including routines that post there. Spend
        is the tenant's debits for the channel, markup included.

        ``window``: ``monthly`` resets each UTC calendar month and takes no
        dates; ``total`` counts everything since ``starts_at`` (or ever);
        ``fixed`` counts only between ``starts_at`` and ``ends_at`` and gates
        nothing outside them. Dates are ISO 8601, read as UTC without an
        offset. A thread id resolves to its parent channel.
        """
        return await _set_channel_budget_impl(
            runtime,
            await _auth(ctx),
            channel_id=channel_id,
            limit_usd=limit_usd,
            window=window,
            starts_at=starts_at,
            ends_at=ends_at,
        )

    @mcp.tool(tags={"admin", *scope_tags("channels:write")})
    async def clear_channel_budget(  # pyright: ignore[reportUnusedFunction]
        ctx: Context, channel_id: str
    ) -> ClearChannelBudgetResult:
        """Remove a channel's spending budget, so only the balance and caps apply. Admin-only.

        A thread id clears its parent channel's budget.
        """
        return await _clear_channel_budget_impl(runtime, await _auth(ctx), channel_id)
