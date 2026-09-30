"""Routines tools: create / list / get / update / delete.

``register_routines_tools(mcp, runtime)`` wires the ``@mcp.tool`` closures for
this group; each closure delegates to a module-private ``_*_impl`` function
that can be unit-tested without a FastMCP Context.

Partition scope: all five tools operate within the tenant from the caller's
JWT claims. Cross-partition access raises ``ToolError("routine not found")``
— same message for unknown vs. forbidden IDs so existence is not leaked.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import cast
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import discord
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools._ctx import _auth  # pyright: ignore[reportPrivateUsage]
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
from daimon.adapters.mcp.tools.slack._client import (
    _require_slack_identity,  # pyright: ignore[reportPrivateUsage]
    _require_team_id,  # pyright: ignore[reportPrivateUsage]
    slack_web_client,
)
from daimon.adapters.mcp.tools.slack._visibility import check_channel_access
from daimon.core.access_policy import TenantAccessPolicy, is_outside_agent_pin, is_write_protected
from daimon.core.cron import next_slot_at_or_after
from daimon.core.defaults.ma_index import find_agent_by_daimon_tag
from daimon.core.routine_delivery import destination_shape_error
from daimon.core.stores import routines as routines_store
from daimon.core.stores.access_policy import AccessPolicyUnreadable, load_access_policy
from daimon.core.stores.domain import CatchUpPolicy, RoutineDestinationKind, RoutineRow
from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from pydantic import BaseModel
from slack_sdk.errors import SlackApiError
from sqlalchemy.ext.asyncio import AsyncSession


class DeleteResult(BaseModel):
    deleted: bool
    routine_id: str


def _require_platform_user_id(auth: AuthIdentity) -> str:
    """Return auth.platform_user_id or raise ToolError if missing.

    Required for create_routine because the scheduler later needs an external_id
    to build a principal for the fired turn (scheduler/main.py:139). CLI sessions
    have no platform_user_id and therefore cannot create schedulable routines.
    """
    if auth.platform_user_id is None:
        raise ToolError("creating a routine requires a platform user identity")
    return auth.platform_user_id


def _require_routine_owner(auth: AuthIdentity, row: RoutineRow) -> None:
    """Owner-or-admin gate for routine mutation. Reads stay tenant-wide.

    Raises the same ``"routine not found"`` text an unknown id produces, so a
    non-owner probe cannot distinguish "forbidden" from "does not exist".

    Both sides of the comparison are nullable and neither null identifies an
    owner: a caller with no platform user id (a CLI-minted token, or an account
    with no principal for its tenant's platform) is nobody's creator, and a
    routine with no recorded creator has no owner to match. Comparing them
    directly would let ``None == None`` through, so both fail closed — only an
    admin may mutate an ownerless routine. Note that a non-admin caller without
    a platform user id therefore cannot mutate any routine.
    """
    if auth.is_admin:
        return
    if auth.platform_user_id is None or row.created_by_user_id is None:
        raise ToolError("routine not found")
    if auth.platform_user_id != row.created_by_user_id:
        raise ToolError("routine not found")


def _compute_next_fire_at(cron_expr: str, tz: str) -> datetime:
    """Validate cron + timezone and return the next fire datetime (UTC).

    This is the ONE legitimate ``except Exception`` site in this module — it
    sits at the MCP boundary and immediately re-raises as ToolError (per
    guideline:architecture named-boundary rule). Croniter raises a mix of
    ValueError / KeyError on bad expressions; catching all is intentional here.
    """
    try:
        ZoneInfo(tz)
    except ZoneInfoNotFoundError as e:
        raise ToolError(f"unknown timezone: {tz!r}") from e
    try:
        return next_slot_at_or_after(cron_expr, tz, datetime.now(UTC))
    except Exception as e:  # croniter raises mixed exception types; named boundary
        raise ToolError(f"invalid cron expression: {cron_expr!r}") from e


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
            f"Slack could not find {destination_id} in this workspace "
            f"({cast('dict[str, object]', err.response.data).get('error')}). Nothing was saved."  # pyright: ignore[reportUnknownMemberType]
        ) from err
    return None, None


async def _check_destination(
    runtime: McpRuntime,
    auth: AuthIdentity,
    *,
    kind: RoutineDestinationKind | None,
    destination_id: str | None,
) -> None:
    """Refuse a destination that could never be posted to, before it is saved.

    The pair must come together; the id must have the platform's shape; the
    channel (and thread) must exist in the caller's own server or workspace
    with daimon able to post; and the tenant access policy must not protect
    it, checked with the parent channel and category resolved here. The
    adapter re-checks all of it at post time, so a channel protected or
    removed later is still honoured.
    """
    if (kind is None) != (destination_id is None):
        raise ToolError("destination_kind and destination_id must be given together")
    if kind is None or destination_id is None:
        return
    platform = auth.platform or ""
    shape_error = destination_shape_error(platform, kind, destination_id)
    if shape_error is not None:
        raise ToolError(f"invalid destination_id: {shape_error}. Nothing was saved.")
    if platform == "discord":
        parent_channel_id, category_id = await _resolve_discord_destination(
            runtime, auth, kind=kind, destination_id=destination_id
        )
        channel_id = destination_id
    else:
        parent_channel_id, category_id = await _resolve_slack_destination(
            runtime, auth, kind=kind, destination_id=destination_id
        )
        channel_id = destination_id.partition(":")[0]
    async with runtime.session_factory() as session:
        try:
            policy = await load_access_policy(session, tenant_id=auth.tenant_id)
        except AccessPolicyUnreadable as err:
            raise ToolError(
                "the workspace access policy could not be read; no destination was saved"
            ) from err
    if is_write_protected(
        policy,
        channel_id=channel_id,
        parent_channel_id=parent_channel_id,
        category_id=category_id,
    ):
        raise ToolError(
            f"{channel_id} is a protected channel: daimon does not post there, so a routine "
            "cannot deliver to it. Pick another channel. Nothing was saved."
        )


async def _load_policy_for_save(session: AsyncSession, *, tenant_id: UUID) -> TenantAccessPolicy:
    try:
        return await load_access_policy(session, tenant_id=tenant_id)
    except AccessPolicyUnreadable as err:
        raise ToolError("the workspace access policy could not be read; nothing was saved") from err


def _check_agent_pin(
    policy: TenantAccessPolicy,
    *,
    platform: str,
    agent_name: str,
    kind: RoutineDestinationKind | None,
    destination_id: str | None,
) -> None:
    """Refuse a routine that would run a pinned agent outside its channels.

    A pinned agent's routine must post straight into one of its pinned
    channels: the scheduler can't resolve a Discord thread's parent at fire
    time, so a thread destination is refused here rather than skipped later.
    The scheduler re-checks at every fire, so a pin added later still holds.
    """
    if agent_name not in policy.agent_channel_pins:
        return
    target_channel_id: str | None = None
    if kind is not None and destination_id is not None:
        if kind == "channel":
            target_channel_id = destination_id
        elif platform == "slack":
            target_channel_id = destination_id.partition(":")[0]
    if is_outside_agent_pin(policy, agent_names=(agent_name,), channel_id=target_channel_id):
        raise ToolError(
            f"{agent_name} is pinned to specific channels by an operator, so its routines "
            "must post straight into one of them (a channel destination, not a thread "
            "or none). Nothing was saved."
        )


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
) -> RoutineRow:
    tenant_id = auth.tenant_id
    platform_user_id = _require_platform_user_id(auth)
    next_fire_at = _compute_next_fire_at(cron_expr, timezone)
    await _check_destination(runtime, auth, kind=destination_kind, destination_id=destination_id)
    async with runtime.session_factory() as session:
        policy = await _load_policy_for_save(session, tenant_id=tenant_id)
    _check_agent_pin(
        policy,
        platform=auth.platform or "",
        agent_name=agent_name,
        kind=destination_kind,
        destination_id=destination_id,
    )

    match = await find_agent_by_daimon_tag(
        runtime.client,
        tenant_id=tenant_id,
        name=agent_name,
    )
    if match is None:
        raise ToolError(f"no agent named {agent_name!r} found for this tenant")
    agent_id = match.id

    async with runtime.session_factory() as session, session.begin():
        return await routines_store.create_routine(
            session,
            tenant_id=tenant_id,
            created_by_user_id=platform_user_id,
            agent_id=agent_id,
            agent_name=agent_name,
            cron_expr=cron_expr,
            timezone_=timezone,
            trigger_message=trigger_message,
            enabled=enabled,
            catch_up_policy=catch_up_policy,
            next_fire_at=next_fire_at,
            destination_kind=destination_kind,
            destination_id=destination_id,
        )


async def _list_routines_impl(
    runtime: McpRuntime,
    auth: AuthIdentity,
) -> list[RoutineRow]:
    tenant_id = auth.tenant_id
    async with runtime.session_factory() as session:
        return await routines_store.list_routines_for_tenant(session, tenant_id=tenant_id)


async def _get_routine_impl(
    runtime: McpRuntime,
    auth: AuthIdentity,
    *,
    routine_id: UUID,
) -> RoutineRow:
    tenant_id = auth.tenant_id
    async with runtime.session_factory() as session:
        row = await routines_store.get_routine(session, routine_id, tenant_id=tenant_id)
    if row is None:
        raise ToolError("routine not found")
    return row


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
    tenant_id = auth.tenant_id
    if clear_destination and (destination_kind is not None or destination_id is not None):
        raise ToolError("clear_destination cannot be combined with a new destination")
    await _check_destination(runtime, auth, kind=destination_kind, destination_id=destination_id)
    async with runtime.session_factory() as session, session.begin():
        row = await routines_store.get_routine(session, routine_id, tenant_id=tenant_id)
        if row is None:
            raise ToolError("routine not found")
        _require_routine_owner(auth, row)
        if clear_destination:
            effective_kind, effective_id = None, None
        elif destination_kind is not None:
            effective_kind, effective_id = destination_kind, destination_id
        else:
            effective_kind, effective_id = row.destination_kind, row.destination_id
        _check_agent_pin(
            await _load_policy_for_save(session, tenant_id=tenant_id),
            platform=auth.platform or "",
            agent_name=agent_name if agent_name is not None else row.agent_name,
            kind=effective_kind,
            destination_id=effective_id,
        )

        # Recompute next_fire_at only when cron or timezone is being changed.
        next_fire_at: datetime | None = None
        if cron_expr is not None or timezone is not None:
            effective_cron = cron_expr if cron_expr is not None else row.cron_expr
            effective_tz = timezone if timezone is not None else row.timezone
            next_fire_at = _compute_next_fire_at(effective_cron, effective_tz)

        # rename support via daimon-tag resolution at tool boundary.
        # If a new agent_name is provided and differs from the current one, look up
        # the live MA agent id and persist both fields. Unknown name -> ToolError.
        new_agent_id: str | None = None
        if agent_name is not None and agent_name != row.agent_name:
            match = await find_agent_by_daimon_tag(
                runtime.client,
                tenant_id=tenant_id,
                name=agent_name,
            )
            if match is None:
                raise ToolError(f"no agent named {agent_name!r} found for this tenant")
            new_agent_id = match.id

        updated = await routines_store.update_routine(
            session,
            routine_id,
            tenant_id=tenant_id,
            cron_expr=cron_expr,
            timezone_=timezone,
            trigger_message=trigger_message,
            enabled=enabled,
            catch_up_policy=catch_up_policy,
            agent_id=new_agent_id,
            agent_name=agent_name if new_agent_id is not None else None,
            next_fire_at=next_fire_at,
            destination_kind=destination_kind,
            destination_id=destination_id,
            clear_destination=clear_destination,
        )
        if updated is None:
            raise ToolError("routine not found")
        return updated


async def _delete_routine_impl(
    runtime: McpRuntime,
    auth: AuthIdentity,
    *,
    routine_id: UUID,
) -> DeleteResult:
    tenant_id = auth.tenant_id
    async with runtime.session_factory() as session, session.begin():
        row = await routines_store.get_routine(session, routine_id, tenant_id=tenant_id)
        if row is None:
            raise ToolError("routine not found")
        _require_routine_owner(auth, row)
        deleted = await routines_store.delete_routine(session, routine_id, tenant_id=tenant_id)
        if not deleted:
            raise ToolError("routine not found")
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
    ) -> RoutineRow:
        """Create a routine in the caller's tenant partition.

        ``catch_up_policy`` is ``skip`` (default) or ``run-once`` after downtime.

        ``destination_kind`` (``channel`` or ``thread``) and ``destination_id``
        optionally name where each run's result goes. The run is told where,
        and if the agent does not post there itself, daimon posts the end of
        its final reply there. On Slack a thread is ``<channel id>:<thread
        ts>``. A protected channel is refused. Without a destination the
        result is only recorded (``last_result_tail``), as before.

        ``agent_name`` MUST be the exact daimon-side display name of an
        existing agent on this tenant (e.g. ``"daimon"``, ``"daimon-copy"``,
        ``"research-bot"``). It is NOT:

        - a free-text description of the routine
        - a mention, tag fragment, or user handle
        - a routine label or trigger phrase
        - the name of a tool, MCP, or skill

        The tool resolves ``agent_name`` to a live MA agent id at the call
        boundary; an unknown name raises ``ToolError`` and the
        routine is not created.

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
        )

    @mcp.tool
    async def list_routines(ctx: Context) -> list[RoutineRow]:  # pyright: ignore[reportUnusedFunction]
        """List all routines in the caller's tenant partition."""
        return await _list_routines_impl(runtime, await _auth(ctx))

    @mcp.tool
    async def get_routine(ctx: Context, routine_id: UUID) -> RoutineRow:  # pyright: ignore[reportUnusedFunction]
        """Get a routine by id (tenant-scoped; raises if not found or cross-tenant)."""
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
        ``create_routine``); ``clear_destination=true`` removes it.

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
