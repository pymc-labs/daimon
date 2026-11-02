"""Routines tools: create / list / get / update / delete.

``register_routines_tools(mcp, runtime)`` wires the ``@mcp.tool`` closures for
this group; each closure delegates to a module-private ``_*_impl`` function
that can be unit-tested without a FastMCP Context.

Partition scope: all five tools operate within the tenant from the caller's
JWT claims, and a non-admin sees and changes only the routines they created.
Cross-partition or someone else's access raises ``ToolError("routine not
found")`` — same message for unknown vs. forbidden IDs so existence is not
leaked.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import cast
from uuid import UUID

import discord
import httpx
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools._authz_facts import mcp_subject
from daimon.adapters.mcp.tools._channel_policy import get_verified_origin, turn_origin_place
from daimon.adapters.mcp.tools._ctx import _auth  # pyright: ignore[reportPrivateUsage]
from daimon.adapters.mcp.tools._rule_view import load_isolation_view
from daimon.adapters.mcp.tools._words import admin_noun
from daimon.adapters.mcp.tools.channel_budgets import origin_budget_channel
from daimon.adapters.mcp.tools.discord._client import (
    _require_bot_token,  # pyright: ignore[reportPrivateUsage]
    _require_discord_identity,  # pyright: ignore[reportPrivateUsage]
    _require_guild_id,  # pyright: ignore[reportPrivateUsage]
    _resolve_member,  # pyright: ignore[reportPrivateUsage]
    rest_client,
)
from daimon.adapters.mcp.tools.discord._visibility import (
    _check_send_permission,  # pyright: ignore[reportPrivateUsage]
    _check_thread_view,  # pyright: ignore[reportPrivateUsage]
    _ensure_thread_parent_cached,  # pyright: ignore[reportPrivateUsage]
)
from daimon.adapters.mcp.tools.setup_target import origin_channel_id
from daimon.adapters.mcp.tools.slack._client import (
    _require_slack_identity,  # pyright: ignore[reportPrivateUsage]
    _require_team_id,  # pyright: ignore[reportPrivateUsage]
    slack_web_client,
)
from daimon.adapters.mcp.tools.slack._visibility import check_channel_access
from daimon.adapters.mcp.tools.teams._directory import (
    graph_for,
    locate_channel,
    require_client,
    split_thread,
)
from daimon.core import routine_service
from daimon.core.routine_service import (
    CallView,
    ChannelAgentName,
    DestinationResolver,
    NewRoutine,
    Placement,
    RoutineCaller,
    RoutineChange,
    RoutineEnv,
    RoutineRefused,
)
from daimon.core.scope import ScopeContext
from daimon.core.stores.domain import CatchUpPolicy, RoutineDestinationKind, RoutineRow
from daimon.core.stores.scoped_config_read import resolve
from daimon.core.teams_graph import GraphUnavailable
from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from pydantic import BaseModel
from slack_sdk.errors import SlackApiError


class DeleteResult(BaseModel):
    deleted: bool
    routine_id: str


async def _resolve_discord_destination(
    runtime: McpRuntime, auth: AuthIdentity, *, kind: str, destination_id: str
) -> tuple[str | None, str | None]:
    """(parent_channel_id, category_id) of a channel or thread in the caller's
    guild that the CALLER may post in.

    A routine posts on its creator's behalf, so its destination passes the
    exact checks the `send_message` tool applies to a caller: the member is
    resolved with roles hydrated, a thread's parent is cached, a thread must
    be viewable (a private one needs membership or manage_threads), and the
    caller needs view + send there.
    """
    guild_id = _require_guild_id(auth)
    caller = _require_discord_identity(auth)
    try:
        async with rest_client(_require_bot_token(runtime)) as client:
            _, member = await _resolve_member(client, guild_id, caller)
            channel = await client.fetch_channel(int(destination_id))
            if isinstance(channel, discord.Thread):
                if kind != "thread":
                    raise ToolError(f"{destination_id} is a thread: use destination_kind=thread")
                parent = await _ensure_thread_parent_cached(channel)
                category = getattr(parent, "category_id", None)
                placement = (str(channel.parent_id), str(category) if category else None)
            elif isinstance(channel, discord.TextChannel):
                if kind != "channel":
                    raise ToolError(f"{destination_id} is a channel: use destination_kind=channel")
                category = channel.category_id
                placement = (None, str(category) if category else None)
            else:
                raise ToolError(f"{destination_id} is not a text channel or thread")
            if str(channel.guild.id) != guild_id:
                raise ToolError(f"{destination_id} is not in this server")
            try:
                if isinstance(channel, discord.Thread):
                    await _check_thread_view(client, channel, member, caller)
                _check_send_permission(channel, member)
            except ToolError as err:
                raise ToolError(
                    f"you cannot post in {destination_id} ({err}), so a routine cannot deliver "
                    "there for you. Nothing was saved."
                ) from err
            return placement
    except (discord.NotFound, discord.Forbidden) as err:
        raise ToolError(
            f"daimon cannot see {destination_id} in this server; check the id and that "
            "daimon has access to it"
        ) from err


async def _resolve_slack_destination(
    runtime: McpRuntime, auth: AuthIdentity, *, kind: str, destination_id: str
) -> tuple[str | None, str | None]:
    """Check the channel (and thread) exists in the caller's workspace, with daimon in it."""
    channel_id, _, thread_ts = destination_id.partition(":")
    client = await slack_web_client(runtime, team_id=_require_team_id(auth))
    try:
        info = await client.conversations_info(channel=channel_id)  # pyright: ignore[reportUnknownMemberType]
        channel = cast("dict[str, object]", info["channel"])
        if channel.get("is_archived"):
            raise ToolError(f"{channel_id} is archived")
        if not channel.get("is_member"):
            raise ToolError(
                f"daimon is not in {channel_id}; invite it to the channel first. Nothing was saved."
            )
        # The routine posts on the caller's behalf: the caller must have the
        # same access the channel tools require of them (private channel or
        # guest → membership).
        try:
            await check_channel_access(
                client, channel=channel, user_id=_require_slack_identity(auth)
            )
        except ToolError as err:
            raise ToolError(
                f"you cannot post in {channel_id} ({err}), so a routine cannot deliver there "
                "for you. Nothing was saved."
            ) from err
        if kind == "thread":
            replies = await client.conversations_replies(  # pyright: ignore[reportUnknownMemberType]
                channel=channel_id, ts=thread_ts, limit=1
            )
            if not cast("list[object]", replies.get("messages") or []):  # pyright: ignore[reportUnknownMemberType]
                raise ToolError(f"no thread {thread_ts} in {channel_id}")
    except SlackApiError as err:
        raise ToolError(
            f"Slack could not find {destination_id} here "
            f"({cast('dict[str, object]', err.response.data).get('error')}). Nothing was saved."  # pyright: ignore[reportUnknownMemberType]
        ) from err
    return None, None


async def _resolve_teams_destination(
    runtime: McpRuntime, auth: AuthIdentity, *, destination_id: str
) -> str:
    """The destination's channel, in a team daimon is in, with the caller on its roster.

    A thread's root must exist; when Graph cannot be read, delivery decides.
    """
    client, caller = require_client(runtime, auth)
    channel_id, root = split_thread(destination_id)
    ref = await locate_channel(runtime, auth, client, channel_id)
    try:
        is_member = await client.is_member(ref.channel_id, caller)
    except (httpx.HTTPError, ValueError) as err:
        raise ToolError(
            f"could not confirm you are in {ref.channel_name}. Nothing was saved."
        ) from err
    if not is_member:
        raise ToolError(
            f"you are not in {ref.channel_name}, so a routine cannot deliver there for you. "
            "Nothing was saved."
        )
    if root is not None:
        try:
            await graph_for(client).get_message(ref.group_id, ref.channel_id, root)
        except GraphUnavailable as err:
            if err.status == 404:
                raise ToolError(
                    f"no thread {root} in {ref.channel_name}. Nothing was saved."
                ) from err
    return ref.channel_id


def _caller(auth: AuthIdentity) -> RoutineCaller:
    return RoutineCaller(
        tenant_id=auth.tenant_id,
        subject=mcp_subject(auth),
        platform=auth.platform or "",
        is_admin=auth.is_admin,
        hold=auth.hold,
        own_agent_ids=frozenset(i for i in (auth.agent_id or auth.chat_agent_id,) if i is not None),
        admin_noun=admin_noun(auth.platform),
    )


def _destination_resolver(runtime: McpRuntime, auth: AuthIdentity) -> DestinationResolver:
    """Looks a destination up on the caller's platform as the caller (see the resolvers above)."""
    platform = auth.platform or ""

    async def resolve(kind: RoutineDestinationKind, destination_id: str) -> Placement:
        if platform == "discord":
            parent_id, category_id = await _resolve_discord_destination(
                runtime, auth, kind=kind, destination_id=destination_id
            )
            return Placement(
                channel_id=destination_id, parent_channel_id=parent_id, category_id=category_id
            )
        if platform == "teams":
            # Teams has no categories; a thread is checked as itself under its channel.
            channel_id = await _resolve_teams_destination(
                runtime, auth, destination_id=destination_id
            )
            return Placement(
                channel_id=destination_id,
                parent_channel_id=None if kind == "channel" else channel_id,
            )
        parent_id, category_id = await _resolve_slack_destination(
            runtime, auth, kind=kind, destination_id=destination_id
        )
        return Placement(
            channel_id=destination_id.partition(":")[0],
            parent_channel_id=parent_id,
            category_id=category_id,
        )

    return resolve


def _channel_agent_name(runtime: McpRuntime, auth: AuthIdentity) -> ChannelAgentName:
    async def name(channel_id: str) -> str | None:
        async with runtime.session_factory() as session:
            config = await resolve(
                session,
                context=ScopeContext(tenant_id=auth.tenant_id, channel_id=channel_id),
                default=runtime.deployment_default,
            )
        return config.agent_name

    return name


def _env(
    runtime: McpRuntime,
    auth: AuthIdentity,
    *,
    origin_context_id: str | None = None,
    verified_origin: bool = False,
) -> RoutineEnv:
    """The service's view of this call. `verified_origin` reads the turn the call names."""

    async def load_view() -> CallView:
        origin = (
            await get_verified_origin(runtime, auth, origin_context_id) if verified_origin else None
        )
        viewer = await load_isolation_view(
            runtime,
            auth,
            location_channel_id=origin_channel_id(origin) if verified_origin else None,
        )
        return CallView(
            viewer=viewer, origin=turn_origin_place(origin) if origin is not None else None
        )

    async def origin_budget() -> str | None:
        return await origin_budget_channel(runtime.session_factory, auth, origin_context_id)

    return RoutineEnv(
        sessionmaker=runtime.session_factory,
        anthropic=runtime.client,
        resolve_destination=_destination_resolver(runtime, auth),
        channel_agent_name=_channel_agent_name(runtime, auth),
        load_view=load_view,
        origin_budget_channel=origin_budget,
        require_destination=runtime.settings.mcp.require_routine_destination,
    )


@contextmanager
def _tool_errors() -> Iterator[None]:
    """The service's plain-text refusals, as the tool error the caller reads."""
    try:
        yield
    except RoutineRefused as err:
        raise ToolError(str(err)) from err


async def _create_routine_impl(
    runtime: McpRuntime,
    auth: AuthIdentity,
    *,
    agent_name: str,
    cron_expr: str,
    timezone: str,
    trigger_message: str,
    enabled: bool = True,
    catch_up_policy: CatchUpPolicy = "skip",
    destination_kind: RoutineDestinationKind | None = None,
    destination_id: str | None = None,
    origin_context_id: str | None = None,
) -> RoutineRow:
    with _tool_errors():
        return await routine_service.create_routine(
            _env(runtime, auth, origin_context_id=origin_context_id, verified_origin=True),
            _caller(auth),
            NewRoutine(
                agent_name=agent_name,
                cron_expr=cron_expr,
                timezone=timezone,
                trigger_message=trigger_message,
                enabled=enabled,
                catch_up_policy=catch_up_policy,
                destination_kind=destination_kind,
                destination_id=destination_id,
            ),
        )


async def _list_routines_impl(
    runtime: McpRuntime,
    auth: AuthIdentity,
) -> list[RoutineRow]:
    with _tool_errors():
        return await routine_service.list_routines(_env(runtime, auth), _caller(auth))


async def _get_routine_impl(
    runtime: McpRuntime,
    auth: AuthIdentity,
    *,
    routine_id: UUID,
) -> RoutineRow:
    with _tool_errors():
        return await routine_service.get_routine(_env(runtime, auth), _caller(auth), routine_id)


async def _update_routine_impl(
    runtime: McpRuntime,
    auth: AuthIdentity,
    *,
    routine_id: UUID,
    agent_name: str | None = None,
    cron_expr: str | None = None,
    timezone: str | None = None,
    trigger_message: str | None = None,
    enabled: bool | None = None,
    catch_up_policy: CatchUpPolicy | None = None,
    destination_kind: RoutineDestinationKind | None = None,
    destination_id: str | None = None,
    clear_destination: bool = False,
) -> RoutineRow:
    with _tool_errors():
        return await routine_service.update_routine(
            _env(runtime, auth),
            _caller(auth),
            routine_id,
            RoutineChange(
                agent_name=agent_name,
                cron_expr=cron_expr,
                timezone=timezone,
                trigger_message=trigger_message,
                enabled=enabled,
                catch_up_policy=catch_up_policy,
                destination_kind=destination_kind,
                destination_id=destination_id,
                clear_destination=clear_destination,
            ),
        )


async def _delete_routine_impl(
    runtime: McpRuntime,
    auth: AuthIdentity,
    *,
    routine_id: UUID,
) -> DeleteResult:
    with _tool_errors():
        await routine_service.delete_routine(_env(runtime, auth), _caller(auth), routine_id)
    return DeleteResult(deleted=True, routine_id=str(routine_id))


def register_routines_tools(mcp: FastMCP, runtime: McpRuntime) -> None:
    @mcp.tool
    async def create_routine(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        agent_name: str,
        cron_expr: str,
        timezone: str,
        trigger_message: str,
        enabled: bool = True,
        catch_up_policy: CatchUpPolicy = "skip",
        destination_kind: RoutineDestinationKind | None = None,
        destination_id: str | None = None,
        origin_context_id: str | None = None,
    ) -> RoutineRow:
        """Create a routine in the caller's tenant partition.

        ``catch_up_policy`` is ``skip`` (default) or ``run-once`` after downtime.

        ``destination_kind`` (``channel`` or ``thread``) and ``destination_id``
        optionally name where each run's result goes. The run is told where,
        and if the agent does not post there itself, daimon posts the end of
        its final reply there. A thread is its id exactly as turn_controls
        or a read tool gives it, and a malformed id is refused with the form to
        use; the channel must be one you are in. A protected channel is refused.
        Without a destination the
        result is only recorded (``last_result_tail``), as before. A run's
        spend counts toward the destination channel's budget, or without a
        destination the budget of the channel named by ``origin_context_id``
        (this turn's), and a run is skipped while that budget is used up.

        ``agent_name`` MUST be the exact daimon-side display name of an
        existing agent on this tenant (e.g. ``"daimon"``, ``"daimon-copy"``,
        ``"research-bot"``). It is NOT:

        - a free-text description of the routine
        - a mention, tag fragment, or user handle
        - a routine label or trigger phrase
        - the name of a tool, MCP, or skill

        The tool resolves ``agent_name`` to a live MA agent id at the call
        boundary; an unknown name raises ``ToolError`` and the
        routine is not created. A non-admin may only name the agent they are
        talking to, or the agent the destination channel answers with.

        If you are the calling agent and do not know which agent to bind the
        routine to, DEFAULT to your own name (the one you were addressed as
        in this conversation). Do NOT guess from context — if ambiguous, ask
        the user which agent should own the routine.

        Example: a user says "daimon, create a routine that pings me every
        5 minutes with the message 'ping'". The correct call is
        ``create_routine(agent_name="daimon", cron_expr="*/5 * * * *",
        timezone="UTC", trigger_message="ping")``.
        """
        return await _create_routine_impl(
            runtime,
            await _auth(ctx),
            agent_name=agent_name,
            cron_expr=cron_expr,
            timezone=timezone,
            trigger_message=trigger_message,
            enabled=enabled,
            catch_up_policy=catch_up_policy,
            destination_kind=destination_kind,
            destination_id=destination_id,
            origin_context_id=origin_context_id,
        )

    @mcp.tool
    async def list_routines(ctx: Context) -> list[RoutineRow]:  # pyright: ignore[reportUnusedFunction]
        """List the routines you created (an admin sees every routine in the workspace)."""
        return await _list_routines_impl(runtime, await _auth(ctx))

    @mcp.tool
    async def get_routine(ctx: Context, routine_id: UUID) -> RoutineRow:  # pyright: ignore[reportUnusedFunction]
        """Get a routine by id. Raises if not found, cross-tenant, or someone else's and you
        are not an admin."""
        return await _get_routine_impl(runtime, await _auth(ctx), routine_id=routine_id)

    @mcp.tool
    async def update_routine(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        routine_id: UUID,
        agent_name: str | None = None,
        cron_expr: str | None = None,
        timezone: str | None = None,
        trigger_message: str | None = None,
        enabled: bool | None = None,
        catch_up_policy: CatchUpPolicy | None = None,
        destination_kind: RoutineDestinationKind | None = None,
        destination_id: str | None = None,
        clear_destination: bool = False,
    ) -> RoutineRow:
        """PATCH-update a routine. Only provided fields are changed.

        ``destination_kind`` + ``destination_id`` set where results go (see
        ``create_routine``); ``clear_destination=true`` removes it, and the
        routine's spend still counts toward the same channel's budget.

        ``catch_up_policy`` selects ``skip`` or one coalesced ``run-once`` after downtime.

        ``agent_name`` reassigns the routine to a different daimon-tagged agent;
        the tool resolves it to a live MA agent id at the call boundary.
        """
        return await _update_routine_impl(
            runtime,
            await _auth(ctx),
            routine_id=routine_id,
            agent_name=agent_name,
            cron_expr=cron_expr,
            timezone=timezone,
            trigger_message=trigger_message,
            enabled=enabled,
            catch_up_policy=catch_up_policy,
            destination_kind=destination_kind,
            destination_id=destination_id,
            clear_destination=clear_destination,
        )

    @mcp.tool
    async def delete_routine(ctx: Context, routine_id: UUID) -> DeleteResult:  # pyright: ignore[reportUnusedFunction]
        """Delete a routine (hard delete, tenant-scoped)."""
        return await _delete_routine_impl(runtime, await _auth(ctx), routine_id=routine_id)
