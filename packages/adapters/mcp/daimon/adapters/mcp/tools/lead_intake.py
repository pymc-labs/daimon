"""First-party lead intake tools; authority derives only from live server context."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

import discord
import httpx
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.services.lead_intake import HttpIntakePorts
from daimon.adapters.mcp.tools._channel_policy import (
    _executing_agent,  # pyright: ignore[reportPrivateUsage]
    _running_hold,  # pyright: ignore[reportPrivateUsage]
    load_channel_policy,
    require_channel_writable,
    turn_origin_place,
)
from daimon.adapters.mcp.tools._ctx import _auth  # pyright: ignore[reportPrivateUsage]
from daimon.adapters.mcp.tools._session_gate import session_card_gap
from daimon.adapters.mcp.tools.discord._client import (
    _resolve_member,  # pyright: ignore[reportPrivateUsage]
    rest_client,
)
from daimon.adapters.mcp.tools.discord._visibility import (
    _check_create_thread_permission,  # pyright: ignore[reportPrivateUsage]
    _check_send_permission,  # pyright: ignore[reportPrivateUsage]
    _check_thread_view,  # pyright: ignore[reportPrivateUsage]
    _check_view_permission,  # pyright: ignore[reportPrivateUsage]
    _ensure_thread_parent_cached,  # pyright: ignore[reportPrivateUsage]
)
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.authz import Action, Place, Subject, authorize
from daimon.core.lead_intake import (
    IntakeConfig,
    IntakeRecord,
    IntakeResolution,
    LeadDraft,
    LeadSource,
    authorize_intake_actor,
)
from daimon.core.lead_intake_access import (
    LeadReadDenied,
    LeadReader,
    LeadReadTarget,
    NativeChannel,
    authorize_lead_read,
    discovery_target,
    read_target,
)
from daimon.core.lead_intake_service import IntakeAccess, IntakeService, VerifiedSource
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.membership import Membership
from daimon.core.permissions import home_of
from daimon.core.stores.accounts import account_exists
from daimon.core.stores.domain import TurnOriginRow
from daimon.core.stores.lead_intake import IntakeStore, execution_is_active
from daimon.core.stores.routines import get_routine
from daimon.core.stores.turn_origins import get_session_origin
from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError


class McpNativeLeadPermissions:
    """Native read facts for one member over the REST client, once per channel."""

    def __init__(self, client: discord.Client, member: discord.Member) -> None:
        self.client, self.member = client, member
        self._seen: dict[str, NativeChannel | None] = {}

    async def channel(self, channel_id: str) -> NativeChannel | None:
        if channel_id not in self._seen:
            self._seen[channel_id] = await self._fetch(channel_id)
        return self._seen[channel_id]

    async def _fetch(self, channel_id: str) -> NativeChannel | None:
        try:
            channel = await self.client.fetch_channel(int(channel_id))
            if isinstance(channel, discord.Thread):
                parent = await _ensure_thread_parent_cached(channel)
                permissions = channel.permissions_for(self.member)
                private = channel.type is discord.ChannelType.private_thread
                member_of = not private or permissions.manage_threads
                if not member_of:
                    try:
                        await channel.fetch_member(self.member.id)
                        member_of = True
                    except discord.NotFound:
                        member_of = False
                return NativeChannel(
                    id=str(channel.id),
                    guild_id=str(channel.guild.id),
                    parent_id=str(channel.parent_id),
                    category_id=str(parent.category_id) if parent.category_id else None,
                    is_text=False,
                    is_thread=True,
                    private_thread=private,
                    everyone_can_view=parent.permissions_for(
                        parent.guild.default_role
                    ).view_channel,
                    can_view=permissions.view_channel,
                    can_read_history=permissions.read_message_history,
                    in_thread_or_manages=member_of,
                )
        except (discord.Forbidden, discord.NotFound, ToolError, ValueError):
            return None
        if not isinstance(channel, discord.abc.GuildChannel):
            return None
        permissions = channel.permissions_for(self.member)
        return NativeChannel(
            id=str(channel.id),
            guild_id=str(channel.guild.id),
            parent_id=None,
            category_id=str(channel.category_id) if channel.category_id else None,
            is_text=isinstance(channel, discord.TextChannel),
            is_thread=False,
            private_thread=False,
            everyone_can_view=channel.permissions_for(channel.guild.default_role).view_channel,
            can_view=permissions.view_channel,
            can_read_history=permissions.read_message_history,
            in_thread_or_manages=True,
        )


async def authorize_effect_destination(
    runtime: McpRuntime,
    auth: AuthIdentity,
    client: discord.Client,
    member: discord.Member,
    config: IntakeConfig,
    policy: TenantAccessPolicy,
    access: IntakeAccess,
    origin_ids: frozenset[str],
) -> None:
    """Check the current destination's platform and Daimon write rules, including its thread.

    Writes only: every read goes through `authorize_lead_read`.
    """
    parent = await client.fetch_channel(int(config.channel_id))
    if not isinstance(parent, discord.TextChannel) or str(parent.guild.id) != config.guild_id:
        raise ToolError("sales_channel_required")
    if parent.permissions_for(parent.guild.default_role).view_channel:
        raise ToolError("restricted_sales_channel_required")
    _check_view_permission(parent, member)
    category = str(parent.category_id) if parent.category_id else None
    targets: list[discord.TextChannel | discord.Thread] = [parent]
    if access.thread_id:
        thread = await client.fetch_channel(int(access.thread_id))
        if (
            not isinstance(thread, discord.Thread)
            or str(thread.guild.id) != config.guild_id
            or str(thread.parent_id) != config.channel_id
        ):
            raise ToolError("dossier_thread_mismatch")
        await _ensure_thread_parent_cached(thread)
        await _check_thread_view(client, thread, member, auth.platform_user_id or "")
        targets.append(thread)
    for target in targets:
        _check_view_permission(target, member)
        parent_id = config.channel_id if isinstance(target, discord.Thread) else None
        if not authorize(
            policy,
            subject=Subject(),
            action=Action.READ_CHANNEL,
            place=Place(
                channel_id=str(target.id), parent_channel_id=parent_id, category_id=category
            ),
            origin_channel_ids=origin_ids,
        ):
            raise ToolError("sales_queue_read_denied")
        _check_send_permission(target, member)
        await require_channel_writable(
            runtime,
            auth,
            channel_id=str(target.id),
            parent_channel_id=parent_id,
            category_id=category,
            policy=policy,
        )
    if access.create_thread:
        _check_create_thread_permission(parent, member)


type CheckRead = Callable[[LeadReadTarget], Awaitable[None]]


@dataclass(frozen=True)
class IntakeSession:
    """One tool call's service, and a factory for its batched read checks."""

    service: IntakeService
    reads: Callable[[], AbstractAsyncContextManager[CheckRead]]

    async def visible(self, records: list[IntakeRecord]) -> list[IntakeRecord]:
        """The records this reader may read; the rest are omitted, never summarized."""
        shown: list[IntakeRecord] = []
        async with self.reads() as check:
            for record in records:
                try:
                    await check(read_target(record))
                except (LeadReadDenied, ToolError):
                    continue
                shown.append(record)
        return shown


@dataclass(frozen=True)
class _Execution:
    """The live facts of the calling execution, re-read for every check."""

    policy: TenantAccessPolicy
    facts: Membership
    routine: bool
    active: bool
    origins: list[TurnOriginRow]
    origin_ids: frozenset[str]
    source_place: Place | None


@asynccontextmanager
async def service(
    runtime: McpRuntime,
    auth: AuthIdentity,
    *,
    resolve: bool = False,
    draft: LeadDraft | None = None,
    read_only: bool = False,
) -> AsyncIterator[IntakeSession]:
    if (
        runtime.fernet is None
        or runtime.settings.discord is None
        or auth.platform != "discord"
        or not auth.platform_user_id
        or not auth.external_id
        or auth.is_operator
        or auth.agent_id is not None
    ):
        raise ToolError("lead_intake_requires_authenticated_discord_chat")
    store = IntakeStore(runtime.session_factory, runtime.fernet, auth.tenant_id)
    try:
        config, credentials = await store.configuration()
    except ValueError as exc:
        raise ToolError("lead_intake_not_configured") from exc
    if auth.external_id != config.guild_id or auth.chat_agent_id != config.agent_id:
        raise ToolError("lead_intake_agent_or_tenant_mismatch")
    token = runtime.settings.discord.bot_token.get_secret_value()
    user_id = auth.platform_user_id

    async def execution(source_ref: LeadSource | None) -> _Execution:
        async with runtime.session_factory() as session:
            if not await account_exists(session, account_id=auth.account_id):
                raise ToolError("actor_account_revoked")
        policy = await load_channel_policy(runtime, auth)
        resolver = runtime.membership_resolver
        if resolver is None:
            raise ToolError("membership_unavailable")
        facts = await resolver.resolve(auth.tenant_id, config.guild_id, user_id, require_fresh=True)
        active = False
        row = None
        routine = auth.routine_id is not None or auth.execution_id is not None
        if routine:
            async with runtime.session_factory() as session:
                row = (
                    await get_routine(session, auth.routine_id, tenant_id=auth.tenant_id)
                    if auth.routine_id
                    else None
                )
                active = (
                    auth.routine_id is not None
                    and auth.execution_id is not None
                    and await execution_is_active(
                        session, auth.tenant_id, auth.routine_id, auth.execution_id
                    )
                )
            if (
                not active
                or row is None
                or not row.enabled
                or row.created_by_user_id != auth.platform_user_id
                or config.routine_id != auth.routine_id
                or resolve
                or (source_ref is not None and source_ref.kind != "gmail")
            ):
                raise ToolError("routine_not_authorized")
        if not auth.chat_session_id:
            raise ToolError("execution_session_grant_required")
        origins: list[TurnOriginRow] = []
        if not routine:
            async with runtime.session_factory() as session:
                origin = await get_session_origin(
                    session,
                    tenant_id=auth.tenant_id,
                    account_id=auth.account_id,
                    platform="discord",
                    chat_session_id=auth.chat_session_id,
                    now=datetime.now(UTC),
                )
            if (
                origin is None
                or not origin.attended
                or derive_agent_uuid(
                    tenant_id=auth.tenant_id, ma_agent_id=origin.responder_ma_agent_id
                )
                != auth.chat_agent_id
            ):
                raise ToolError("attended_execution_origin_required")
            origins = [origin]
            if (
                resolve
                and await session_card_gap(runtime, auth, origin, tool_name="lead_intake_resolve")
                is not None
            ):
                raise ToolError("human_resolution_approval_required")
        origin_ids = {value for o in origins for value in (o.parent_channel_id, o.thread_id)}
        source_place = None
        if routine and row is not None:
            origin_ids.update(
                value for value in (row.source_channel_id, row.source_thread_id) if value
            )
            source_place = Place.from_origin(
                parent_channel_id=row.source_channel_id, thread_id=row.source_thread_id
            )
            # Sealed sources cannot be exported to a CRM by a scheduled agent.
            if home_of(
                policy, row.source_thread_id or row.source_channel_id, row.source_channel_id
            ):
                raise ToolError("isolated_source_export_denied")
        if any(home_of(policy, o.parent_channel_id, o.thread_id) for o in origins):
            raise ToolError("isolated_source_export_denied")
        return _Execution(
            policy=policy,
            facts=facts,
            routine=routine,
            active=active,
            origins=origins,
            origin_ids=frozenset(origin_ids),
            source_place=source_place,
        )

    async def reauthorize(access: IntakeAccess) -> None:
        source_ref = access.source
        live = await execution(source_ref)
        try:
            authorize_intake_actor(
                config,
                live.policy,
                user_id=user_id,
                facts=live.facts,
                agent_id=auth.chat_agent_id,
                routine_id=auth.routine_id,
                execution_active=live.active if live.routine else False,
                resolve=resolve,
            )
        except PermissionError as exc:
            raise ToolError(str(exc)) from None
        if not access.write:
            # Reads authorize each record or discovery through `reads`.
            return
        # Re-read destination membership and thread visibility, and check platform
        # and Daimon write rules before every recovery and each write.
        async with rest_client(token) as client:
            _, member = await _resolve_member(client, config.guild_id, user_id)
            await authorize_effect_destination(
                runtime, auth, client, member, config, live.policy, access, live.origin_ids
            )
            if source_ref is not None and source_ref.kind == "manual":
                source = await client.fetch_channel(int(source_ref.channel_id or "0"))
                if (
                    not isinstance(source, discord.TextChannel | discord.Thread)
                    or str(source.guild.id) != config.guild_id
                ):
                    raise ToolError("manual_source_channel_required")
                parent_id = None
                if isinstance(source, discord.Thread):
                    await _ensure_thread_parent_cached(source)
                    await _check_thread_view(client, source, member, user_id)
                    parent_id = str(source.parent_id)
                _check_view_permission(source, member)
                decision = authorize(
                    live.policy,
                    subject=Subject(),
                    action=Action.READ_CHANNEL,
                    place=Place(channel_id=str(source.id), parent_channel_id=parent_id),
                    origin_channel_ids=frozenset(
                        value
                        for origin in live.origins
                        for value in (origin.parent_channel_id, origin.thread_id)
                    ),
                )
                if not decision or home_of(live.policy, str(source.id), parent_id):
                    raise ToolError("source_export_denied")

    @asynccontextmanager
    async def reads() -> AsyncIterator[CheckRead]:
        """One live reader (executing agent and hold) for a batch of reads."""
        live = await execution(None)
        agent = await _executing_agent(runtime, auth, live.policy)
        hold = await _running_hold(runtime, auth, live.policy, agent)
        origin = hold or (turn_origin_place(live.origins[0]) if live.origins else live.source_place)
        reader = LeadReader(
            tenant_id=auth.tenant_id,
            platform_user_id=user_id,
            facts=live.facts,
            policy=live.policy,
            config=config,
            agent_id=auth.chat_agent_id,
            agent=agent,
            origin=origin,
            origin_channel_ids=live.origin_ids,
            routine_id=auth.routine_id,
            execution_active=live.active if live.routine else False,
            resolve=resolve,
        )
        async with rest_client(token) as client:
            _, member = await _resolve_member(client, config.guild_id, user_id)
            native = McpNativeLeadPermissions(client, member)

            async def check(target: LeadReadTarget) -> None:
                await authorize_lead_read(target, reader, native)

            yield check

    async def authorize_read(target: LeadReadTarget) -> None:
        async with reads() as check:
            try:
                await check(target)
            except LeadReadDenied as exc:
                raise ToolError(str(exc)) from None

    await reauthorize(IntakeAccess(source=draft.source if draft else None, write=not read_only))
    async with httpx.AsyncClient(timeout=30, follow_redirects=False) as client:
        refresh = None
        if credentials.get("gmail_refresh_token"):
            refresh = {
                "refresh_token": credentials["gmail_refresh_token"],
                "client_id": credentials["gmail_client_id"],
                "client_secret": credentials["gmail_client_secret"],
            }
        ports = HttpIntakePorts(
            client,
            gmail_token=credentials.get("gmail_access_token", ""),
            hubspot_token=credentials["hubspot_access_token"],
            discord_token=token,
            gmail_refresh_credentials=refresh,
        )
        provenance = (
            (auth.routine_id, auth.execution_id) if auth.routine_id and auth.execution_id else None
        )
        yield IntakeSession(
            IntakeService(
                store, ports, reauthorize, authorize_read=authorize_read, provenance=provenance
            ),
            reads,
        )


def register_lead_intake_tools(mcp: FastMCP, runtime: McpRuntime) -> None:
    @mcp.tool(tags={"discord"})
    async def lead_intake_submit(  # pyright: ignore[reportUnusedFunction]
        ctx: Context, draft: LeadDraft
    ) -> IntakeRecord:
        """Verify and durably register one classified Gmail or own explicit Discord lead.

        Classify spam and out-of-scope messages as other. Capture has no external
        effects. The service posts a title and threaded dossier, writes only a
        draft reply, conservatively reuses/creates CRM records, then archives
        Gmail after dossier delivery. Replay the same immutable source on timeout.
        The record keeps the sales channel configured now: only people who can
        read that channel's history, and its dossier thread, can read it later.
        """
        auth = await _auth(ctx)
        async with service(runtime, auth, draft=draft) as intake:
            return await intake.service.submit(draft, auth.platform_user_id)

    @mcp.tool(tags={"discord"})
    async def lead_intake_resume(  # pyright: ignore[reportUnusedFunction]
        ctx: Context, intake_id: UUID | None = None
    ) -> dict[str, object]:
        """Recover lead effects and mailbox coverage without blindly retrying uncertain creates.

        Without an ID, resumes up to 25 pending records and scans archived mail
        from the operator checkpoint. Inspect scan errors and remaining work.
        Records the caller may not read are omitted from the result.
        """
        async with service(runtime, await _auth(ctx)) as intake:
            if intake_id:
                record = await intake.service.resume(intake_id)
                return {"intake": record.model_dump(mode="json")}
            config, _ = await intake.service.store.configuration()
            async with intake.reads() as check:
                pending = await intake.service.store.list(
                    limit=26,
                    pending_only=True,
                    destination_channel_id=config.channel_id,
                    config_revision=config.revision,
                    authorize_read=check,
                )
            results = [await intake.service.advance(r.id) for r in pending[:25]]
            return {
                "intakes": [r.model_dump(mode="json") for r in await intake.visible(results)],
                "has_more_pending": len(pending) > 25,
                "mailbox_scan": await intake.service.scan(),
            }

    @mcp.tool(tags={"discord"})
    async def lead_intake_read_source(  # pyright: ignore[reportUnusedFunction]
        ctx: Context, message_id: str
    ) -> VerifiedSource:
        """Read attachment-free original Gmail text for an unexpired discovered message.

        Run resume to discover mail, read each source here, then classify and
        submit its immutable source ID. Uses the configured tenant mailbox;
        credentials never enter the result. Does not post, archive or change CRM.
        Readers are those of the sales channel configured now.
        """
        async with service(runtime, await _auth(ctx), read_only=True) as intake:
            return await intake.service.read_source(message_id)

    @mcp.tool(tags={"discord"})
    async def lead_intake_resolve(  # pyright: ignore[reportUnusedFunction]
        ctx: Context, intake_id: UUID, resolution: IntakeResolution
    ) -> IntakeRecord:
        """Apply evidence-backed human CRM selections as a sales resolver or core-role member.

        Show exact records and proposed writes before approval. Routines never
        resolve. Completed identities are immutable; uncertain creates must be
        recovered before selecting replacements. Server admin alone is no grant.
        The resolver must also be able to read the lead.
        """
        auth = await _auth(ctx)
        async with service(runtime, auth, resolve=True) as intake:
            return await intake.service.resolve(intake_id, resolution, auth.platform_user_id or "")

    @mcp.tool(tags={"discord"})
    async def lead_intake_get(  # pyright: ignore[reportUnusedFunction]
        ctx: Context, intake_id: UUID
    ) -> IntakeRecord:
        """Read one authorized tenant lead, its review state and durable effect progress.

        Readers are fixed when the lead is captured: those who can read the sales
        channel it was captured for, and its dossier thread.
        """
        async with service(runtime, await _auth(ctx), read_only=True) as intake:
            return await intake.service.get(intake_id)

    @mcp.tool(tags={"discord"})
    async def lead_intake_list(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        after_id: UUID | None = None,
    ) -> list[IntakeRecord]:
        """Read up to 100 tenant leads the caller may read, oldest first.

        Leads the caller may not read are omitted; pass the last listed ID as
        after_id for the next page.
        """
        async with (
            service(runtime, await _auth(ctx), read_only=True) as intake,
            intake.reads() as check,
        ):
            return await intake.service.store.list(after_id=after_id, authorize_read=check)

    @mcp.tool(tags={"discord"})
    async def lead_intake_health(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
    ) -> dict[str, object]:
        """Read tenant lead counts, oldest pending age and mailbox coverage.

        Readers are those of the sales channel configured now.
        """
        auth = await _auth(ctx)
        async with service(runtime, auth, read_only=True) as intake:
            store = intake.service.store
            config, _ = await store.configuration()
            await intake.service.authorize_read(discovery_target(config, auth.tenant_id))
            state = await store.mailbox(config)
            return {**await store.health(), "mailbox": state.model_dump(mode="json")}
