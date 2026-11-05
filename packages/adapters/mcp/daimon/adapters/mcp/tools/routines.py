"""Routines tools: create / list / get / update / delete, and an admin's sandbox opt-in.

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

from collections.abc import Awaitable, Callable, Iterator
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
from daimon.adapters.mcp.tools._web_url import WebUrl, tenant_links
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
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.admin_actions import DelegatedAction
from daimon.core.authz import Action, Subject, authorize, build_subject
from daimon.core.discord_membership import verify_discord_source
from daimon.core.errors import StoreError
from daimon.core.membership_subject import resolve_subject
from daimon.core.permissions import dm_source_limited
from daimon.core.routine_sandbox import SandboxPinUnavailable, current_pin
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
    RoutineSource,
    SourceResolver,
)
from daimon.core.routines import routine_for_reader
from daimon.core.scope import ScopeContext
from daimon.core.security_audit import record_authz_denial, record_policy_decision
from daimon.core.stores import routines as routines_store
from daimon.core.stores.access_policy import AccessPolicyUnreadable, load_access_policy
from daimon.core.stores.direct_messages import DM_SCOPE_PREFIX, get_conversation_by_scope
from daimon.core.stores.domain import (
    CatchUpPolicy,
    RoutineDestinationKind,
    RoutineRow,
    TurnOriginRow,
)
from daimon.core.stores.scoped_config_read import resolve
from daimon.core.teams_graph import GraphUnavailable
from daimon.core.web_links import TenantLinks
from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from pydantic import BaseModel
from slack_sdk.errors import SlackApiError
from sqlalchemy.ext.asyncio import AsyncSession


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
        is_admin=auth.admin_for(DelegatedAction.ROUTINES),
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


_UNSOURCED = "The private conversation's source could not be established."


def _source_resolver(
    runtime: McpRuntime,
    auth: AuthIdentity,
    origin_context_id: str | None,
    verified: Callable[[], Awaitable[TurnOriginRow | None]],
) -> SourceResolver:
    """Where a chat-made routine comes from: the channel and thread of the turn it names.

    A routine made in a private conversation comes from the channel that conversation
    was moved from. On Discord, a DM routine's source, and any source not read from a
    verified turn, is checked against the server.
    """

    async def resolve(
        kind: RoutineDestinationKind | None, destination_id: str | None, budget: str | None
    ) -> RoutineSource:
        origin = await verified()
        if origin_context_id is not None and origin is None:
            raise ToolError("The routine's source origin could not be verified.")
        channel_id = budget
        thread_id = destination_id if kind == "thread" else None
        source_id: str | None = None
        private = (
            auth.platform == "discord"
            and origin is not None
            and origin.thread_id.startswith(DM_SCOPE_PREFIX)
        )
        if private and origin is not None:
            user_id = auth.platform_user_id
            async with runtime.session_factory() as session:
                conversation = await get_conversation_by_scope(
                    session,
                    tenant_id=auth.tenant_id,
                    account_id=auth.account_id,
                    scope_id=origin.thread_id,
                )
                policy = await _policy(session, auth)
            if (
                conversation is None
                or conversation.external_user_id != user_id
                or conversation.platform != "discord"
                or conversation.workspace_id != _require_guild_id(auth)
            ):
                raise ToolError(_UNSOURCED)
            if conversation.source_channel_id is None:
                if (
                    not conversation.is_cold
                    or conversation.source_thread_id is not None
                    or conversation.source_thread_keys
                ):
                    raise ToolError(_UNSOURCED)
                channel_id, thread_id = None, None
            else:
                source_id = conversation.source_thread_id or conversation.source_channel_id
                channel_id = conversation.source_channel_id
                thread_id = conversation.source_thread_id
                if kind == "dm" and dm_source_limited(
                    policy,
                    source_channel_id=channel_id,
                    source_thread_id=thread_id,
                    source_thread_keys=conversation.source_thread_keys or (),
                ):
                    raise ToolError("A routine from a restricted source cannot deliver by DM.")
        elif origin is not None and not origin.thread_id.startswith(DM_SCOPE_PREFIX):
            channel_id = origin.parent_channel_id
            thread_id = origin.thread_id if origin.thread_id != origin.parent_channel_id else None
            source_id = thread_id or channel_id
        elif kind in (None, "dm"):
            source_id = channel_id
        if source_id is not None and (
            kind == "dm" or private or (origin is None and auth.platform == "discord")
        ):
            try:
                verified_channel, thread_id = await verify_discord_source(
                    _require_bot_token(runtime),
                    guild_id=_require_guild_id(auth),
                    channel_id=source_id,
                )
            except StoreError as exc:
                raise ToolError(str(exc)) from exc
            if private and verified_channel != channel_id:
                raise ToolError("The private conversation's source parent could not be verified.")
            channel_id = verified_channel
        return RoutineSource(channel_id=channel_id, thread_id=thread_id)

    return resolve


async def _policy(session: AsyncSession, auth: AuthIdentity) -> TenantAccessPolicy:
    try:
        return await load_access_policy(session, tenant_id=auth.tenant_id)
    except AccessPolicyUnreadable as err:
        raise ToolError("the access settings here could not be read; nothing was saved") from err


def _env(
    runtime: McpRuntime,
    auth: AuthIdentity,
    *,
    origin_context_id: str | None = None,
    verified_origin: bool = False,
) -> RoutineEnv:
    """The service's view of this call. `verified_origin` reads the turn the call names."""
    origins: list[TurnOriginRow | None] = []

    async def verified() -> TurnOriginRow | None:
        if not origins:
            origins.append(await get_verified_origin(runtime, auth, origin_context_id))
        return origins[0]

    async def load_view() -> CallView:
        origin = await verified() if verified_origin else None
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
        resolve_source=_source_resolver(runtime, auth, origin_context_id, verified)
        if verified_origin
        else None,
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


_SANDBOX_PERSON_ONLY = (
    "A routine's sandbox tools are changed by the operator with "
    "`daimon routines sandbox-tools`, the supported way today. This tool does not "
    "accept an agent, an agent in a conversation, an operator token or a legacy "
    "token. Do not retry."
)
_SANDBOX_ADMIN_ONLY = (
    "A routine's sandbox tools are changed by the operator with "
    "`daimon routines sandbox-tools`, the supported way today. This tool accepts "
    "only a live platform admin's own token, which no Daimon surface issues "
    "yet; the routine's owner cannot. Do not retry."
)


async def _sandbox_admin(runtime: McpRuntime, auth: AuthIdentity) -> Subject:
    """The caller, only if they are a person and a Discord Daimon admin right now.

    An allowlist, not a denylist: the token must carry a jti (no legacy
    vault token from before the sweep), must not be an operator token, an
    agent key or an agent in a conversation, and its holder must be a
    verified member and admin of the Discord server on a fresh membership
    read. Any other platform fails closed, as `watches.fresh_admin` does; the
    operator uses the CLI there.
    """
    if (
        auth.token_jti is None
        or auth.is_operator
        or auth.agent_id is not None
        or auth.chat_agent_id is not None
    ):
        record_authz_denial(Action.SET_ROUTINE_SANDBOX_TOOLS, "person_only")
        raise ToolError(_SANDBOX_PERSON_ONLY)
    admin = False
    if (
        auth.admin_for(DelegatedAction.ROUTINES)
        and auth.platform == "discord"
        and auth.platform_user_id is not None
    ):
        live = await resolve_subject(
            runtime.session_factory,
            runtime.membership_resolver,
            tenant_id=auth.tenant_id,
            user_id=auth.platform_user_id,
            require_fresh=True,
            action=DelegatedAction.ROUTINES,
        )
        admin = live.is_admin and live.verified_membership is True
    subject = build_subject(is_admin=admin, platform_user_id=auth.platform_user_id)
    decision = authorize(
        TenantAccessPolicy(), subject=subject, action=Action.SET_ROUTINE_SANDBOX_TOOLS
    )
    if not decision:
        record_authz_denial(Action.SET_ROUTINE_SANDBOX_TOOLS, decision.reason)
        raise ToolError(_SANDBOX_ADMIN_ONLY)
    record_policy_decision(Action.SET_ROUTINE_SANDBOX_TOOLS, "allow")
    return subject


async def _set_routine_sandbox_tools_impl(
    runtime: McpRuntime,
    auth: AuthIdentity,
    *,
    routine_id: UUID,
    enabled: bool,
) -> RoutineRow:
    actor = await _sandbox_admin(runtime, auth)
    viewer = await load_isolation_view(runtime, auth)
    async with runtime.session_factory() as session:
        row = await routines_store.get_routine(session, routine_id, tenant_id=auth.tenant_id)
    if row is None or not routine_service.sees_routine(viewer, row):
        raise ToolError(routine_service.NOT_FOUND)
    # The opt-in covers the agent version and environment the routine would
    # run with now; the scheduler keeps built-ins off once either changes.
    pin = None
    if enabled:
        try:
            pin = await current_pin(
                runtime.client, runtime.session_factory, row, default=runtime.deployment_default
            )
        except SandboxPinUnavailable as exc:
            raise ToolError(routines_store.PIN_UNAVAILABLE_REFUSAL) from exc
    async with runtime.session_factory.begin() as session:
        try:
            result = await routines_store.set_sandbox_tools(
                session,
                routine_id,
                tenant_id=auth.tenant_id,
                enabled=enabled,
                set_by=str(auth.account_id),
                actor=actor,
                via="mcp",
                pin=pin,
            )
        except StoreError as exc:
            raise ToolError(str(exc)) from exc
    if result is None:
        raise ToolError(routine_service.NOT_FOUND)
    return routine_for_reader(result[0], user_id=auth.platform_user_id)


class RoutineView(RoutineRow):
    """A routine as the tools return it, with its page on the web when there is one."""

    web_url: WebUrl = None


async def _viewed(runtime: McpRuntime, auth: AuthIdentity, row: RoutineRow) -> RoutineView:
    """`row` with its drawer on the web, when the workspace has one for this caller."""
    return _with_link(row, await tenant_links(runtime, auth))


def _with_link(row: RoutineRow, links: TenantLinks | None) -> RoutineView:
    """`row` with its drawer, from links resolved once for the whole call."""
    view = RoutineView.model_validate(row, from_attributes=True)
    return view if links is None else view.model_copy(update={"web_url": links.routine(row.id)})


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
    ) -> RoutineView:
        """Create a recurring routine; owner DMs run as unattended live-DM turns.

        Owner-DM routines are scheduled live-DM turns, using the owner's normal
        personal/shared memory, credentials and DM tool authority in a fresh private
        scope. They support Managed Agents only and run unattended: calls needing
        a person are refused, and nobody watches intermediate actions. Permitted
        tools retain ordinary DM side-effect risk. Crash and stop guarantees are
        exactly those of live DMs. A slot skips if the owner has a DM turn running.
        A routine with no destination or channel also mounts its owner's personal
        memory, read-only; every other routine mounts shared memory only. Sources
        must be supported
        channels/threads in this tenant's guild; the original thread and parent
        remain subject to reader restrictions.

        ``catch_up_policy`` is ``skip`` (default) or ``run-once`` after downtime.

        ``destination_kind`` (``channel``, ``thread`` or ``dm``) and ``destination_id``
        optionally name where each run's result goes. Channel/thread runs are
        told where; if the agent does not post there itself, Daimon posts its full
        final reply there. Owner-DM runs queue their successful final reply
        privately for the owner. Use the canonical thread identifier returned by
        this platform's read tools, in a channel you are in. Where DM delivery is
        supported, ``dm`` delivers only to the authenticated owner; omit
        ``destination_id`` to derive their user ID. Unsupported destinations are
        refused. Delivery status and failure reasons are returned by list/get.
        A protected channel is refused. Without a destination the
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
        auth = await _auth(ctx)
        row = await _create_routine_impl(
            runtime,
            auth,
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
        return await _viewed(runtime, auth, row)

    @mcp.tool
    async def list_routines(ctx: Context) -> list[RoutineView]:  # pyright: ignore[reportUnusedFunction]
        """List the routines you created (an admin sees every routine in the workspace)."""
        auth = await _auth(ctx)
        rows = await _list_routines_impl(runtime, auth)
        links = await tenant_links(runtime, auth)
        return [_with_link(row, links) for row in rows]

    @mcp.tool
    async def get_routine(ctx: Context, routine_id: UUID) -> RoutineView:  # pyright: ignore[reportUnusedFunction]
        """Get a routine by id. Raises if not found, cross-tenant, or someone else's and you
        are not an admin."""
        auth = await _auth(ctx)
        return await _viewed(
            runtime, auth, await _get_routine_impl(runtime, auth, routine_id=routine_id)
        )

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
    ) -> RoutineView:
        """PATCH-update a routine, including owner-only DM delivery where supported.

        Only provided fields are changed.

        ``destination_kind`` + ``destination_id`` set where results go (see
        ``create_routine``); ``clear_destination=true`` removes it, and the
        routine's spend still counts toward the same channel's budget. Saved
        creation-time source channel/thread restrictions survive every edit.
        Legacy routines with unknown provenance keep ordinary destination edits
        but must be recreated from the source channel/thread to select DM delivery.

        ``catch_up_policy`` selects ``skip`` or one coalesced ``run-once`` after downtime.

        ``agent_name`` reassigns the routine to a different daimon-tagged agent;
        the tool resolves it to a live MA agent id at the call boundary.
        """
        auth = await _auth(ctx)
        row = await _update_routine_impl(
            runtime,
            auth,
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
        return await _viewed(runtime, auth, row)

    @mcp.tool
    async def delete_routine(ctx: Context, routine_id: UUID) -> DeleteResult:  # pyright: ignore[reportUnusedFunction]
        """Delete a routine (hard delete, tenant-scoped)."""
        return await _delete_routine_impl(runtime, await _auth(ctx), routine_id=routine_id)

    @mcp.tool(tags={"admin"})
    async def set_routine_sandbox_tools(  # pyright: ignore[reportUnusedFunction]
        ctx: Context, routine_id: UUID, enabled: bool
    ) -> RoutineRow:
        """Turn built-in sandbox tools (bash, file reads and edits and the rest) on or off
        for one routine's own scheduled runs. Admin only.

        They are off for every unattended run by default. The supported way today is the
        operator's ``daimon routines sandbox-tools``. This tool accepts only a live platform
        admin's own token, re-checked against the server on each call; no Daimon
        surface issues such a token yet, and the tool is kept for a future admin surface.
        Owning the routine is not enough, and agent keys, agents in a conversation, operator
        tokens, legacy tokens and other platforms are refused. DM routines cannot be opted
        in. An opt-in covers exactly the routine's current agent version and environment:
        once either changes, runs go without built-ins and the opt-in is cleared. Each
        change records who made it and when, and ``get_routine``/``list_routines`` show it
        as ``sandbox_tools_enabled``. It takes effect from the routine's next run. Setting
        the value a routine already has changes nothing. Timers, watches, agent chat,
        ``daimon run`` and checkpoints cannot be opted in.
        """
        return await _set_routine_sandbox_tools_impl(
            runtime, await _auth(ctx), routine_id=routine_id, enabled=enabled
        )
