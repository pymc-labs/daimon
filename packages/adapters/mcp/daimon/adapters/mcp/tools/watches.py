"""Owner-bound watch management. Secrets travel only in a private owner DM.

Enrollment: every create, resume and signing-key rotation requires a
verified attended owner origin and server-side proof of that owner's card
approval for that exact tool via session_card_gap before any mutation or secret
delivery. Backend or session gating alone is not proof. A backend without
tool_confirmation, an ungated session or an unreadable proof refuses enrollment.
"""

from __future__ import annotations

import asyncio
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Literal, cast
from urllib.parse import urlsplit

import discord
from daimon.adapters.mcp import watch_polling as polling
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools import _session_gate
from daimon.adapters.mcp.tools._channel_policy import require_dm_recipient_allowed
from daimon.adapters.mcp.tools._ctx import _auth  # pyright: ignore[reportPrivateUsage]
from daimon.adapters.mcp.tools.discord._client import (
    _require_bot_token,  # pyright: ignore[reportPrivateUsage]
    rest_client,
)
from daimon.adapters.mcp.tools.setup_target import require_turn_origin
from daimon.adapters.mcp.watch_polling import (
    CredentialMissing,
    PollAccessRevoked,
    attached_server,
    authorize_poll,
    listed_read_tool,
    operator_denied,
    poll_credential,
)
from daimon.core import channel_backend
from daimon.core.agent_mcp_credentials import resolve_hidden_mcp_server_names
from daimon.core.config import DirectMessagePolicy
from daimon.core.continuity.continuation import ContinuationRequest
from daimon.core.continuity.poll_tools import catalogue_digest, require_listed, validate_mcp_poll
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.mcp_server_url import ascii_mcp_url, same_ascii_mcp_url
from daimon.core.mcp_tool_call import McpCallError
from daimon.core.membership_subject import resolve_subject
from daimon.core.mux_backend import resource_scope
from daimon.core.mux_compat import retrieve_agent
from daimon.core.stores import mux_state, watches
from daimon.core.stores.domain import ChatPlatform, TurnOriginRow, WatchRow
from daimon.core.tool_safety import DAIMON_SERVER_NAME
from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from mux.contracts.config import BackendConfig, resolve_default
from mux.contracts.ids import ChannelRef, ThreadRef
from mux.contracts.profile import SUPPORTED
from mux.profiles import get_profile
from mux.state.lease import Slot
from pydantic import BaseModel, JsonValue, TypeAdapter


class WatchSummary(BaseModel):
    watch_id: uuid.UUID
    source: str
    status: str
    reason: str | None
    thread_id: str
    parent_channel_id: str
    agent_id: str
    expires_at: datetime
    key_id: str | None = None
    source_host: str | None = None


def summary(row: WatchRow, *, key_id: str | None = None) -> WatchSummary:
    reason = row.source_state.get("reason")
    server_url = row.source_state.get("server_url") if row.source == "mcp_poll" else None
    return WatchSummary(
        watch_id=row.id,
        source=row.source,
        status=row.status,
        reason=str(reason) if reason else None,
        thread_id=row.thread_id,
        parent_channel_id=row.parent_channel_id,
        agent_id=row.agent_id,
        expires_at=row.expires_at,
        key_id=key_id,
        source_host=urlsplit(str(server_url)).hostname if server_url else None,
    )


class WatchListEntry(WatchSummary):
    """One listed watch: who owns it and how it is doing. No instructions, keys or evidence."""

    owner: str
    owner_id: str
    source_tool: str | None
    destination: str
    fires_today: int
    last_fired_at: datetime | None
    errors: int
    retry_after: datetime | None


#: A watch owner with no stored name on this platform.
OWNER_UNNAMED = "another member"


async def list_watches_impl(
    runtime: McpRuntime, auth: AuthIdentity, *, scope: Literal["mine", "all"] = "mine"
) -> list[WatchListEntry]:
    from daimon.core.stores.platform_names import get_user_names

    if scope == "all" and not await fresh_admin(runtime, auth):
        raise ToolError("Only an admin can see every watch. Ask an admin or an owner.")
    async with runtime.session_factory() as db:
        listed = await watches.list_tenant_watches(
            db,
            tenant_id=auth.tenant_id,
            now=datetime.now(UTC),
            owner_account_id=auth.account_id if scope == "mine" else None,
        )
        names: dict[tuple[str, str], str | None] = {}
        for platform in {item.watch.platform for item in listed}:
            found = await get_user_names(
                db,
                tenant_id=auth.tenant_id,
                platform=platform,
                user_ids={
                    item.watch.owner_external_id
                    for item in listed
                    if item.watch.platform == platform
                },
            )
            names.update({(platform, user): name.label for user, name in found.items()})
    return [list_entry(item, auth=auth, names=names) for item in listed]


def list_entry(
    item: watches.WatchActivity,
    *,
    auth: AuthIdentity,
    names: dict[tuple[str, str], str | None],
) -> WatchListEntry:
    row = item.watch
    tool = row.source_state.get("tool")
    if row.owner_account_id == auth.account_id:
        owner = "you"
    else:
        owner = names.get((row.platform, row.owner_external_id)) or OWNER_UNNAMED
    return WatchListEntry(
        **summary(row).model_dump(),
        owner=owner,
        owner_id=row.owner_external_id,
        source_tool=str(tool) if tool else None,
        destination=f"{row.platform} thread",
        fires_today=item.fires_today,
        last_fired_at=item.last_fired_at,
        errors=row.consecutive_errors,
        retry_after=row.backoff_until,
    )


async def private_key_delivery(
    runtime: McpRuntime,
    auth: AuthIdentity,
    watch: WatchRow,
    *,
    key_id: str,
    key: str,
) -> None:
    """Never return plaintext keys to an MCP result or post into a conversation."""
    policy = runtime.settings.direct_message_policies.get(watch.tenant_id, DirectMessagePolicy())
    if not policy.allows(watch.owner_external_id):
        raise ToolError("Private owner delivery is disabled")
    await require_dm_recipient_allowed(runtime, auth, recipient_id=watch.owner_external_id)
    async with rest_client(_require_bot_token(runtime)) as client:
        # Guild membership is fetched live for the owner.
        if auth.external_id is None:
            raise ToolError("Missing team")
        guild = await client.fetch_guild(int(auth.external_id))
        member = await guild.fetch_member(int(watch.owner_external_id))
        if member.bot:
            raise ToolError("Watch keys require a human owner")
        dm = await member.create_dm()
        await dm.send(
            f"Page signing key for watch {watch.id}, page {watch.page_id}:\n"
            f"key_id={key_id}\nkey={key}\n"
            "Install it privately on the page host and sidecar. This key is shown once.",
            allowed_mentions=discord.AllowedMentions.none(),
        )


async def fresh_admin(runtime: McpRuntime, auth: AuthIdentity) -> bool:
    if not auth.admin_for():
        return False
    if auth.platform != "discord" or auth.platform_user_id is None:
        return False
    subject = await resolve_subject(
        runtime.session_factory,
        runtime.membership_resolver,
        tenant_id=auth.tenant_id,
        user_id=auth.platform_user_id,
        require_fresh=True,
    )
    return subject.verified_membership is True and subject.is_admin


async def require_attended_owner(
    runtime: McpRuntime, auth: AuthIdentity, origin_context_id: str | None, *, tool_name: str
) -> TurnOriginRow:
    if origin_context_id is None:
        raise ToolError("An active reply from the owner is required")
    origin = await require_turn_origin(runtime, auth, origin_context_id)
    if not origin.attended or auth.platform != "discord" or auth.platform_user_id is None:
        raise ToolError("An attended owner conversation is required")
    try:
        slot = Slot(
            thread=ThreadRef(
                channel=ChannelRef(
                    tenant_id=str(auth.tenant_id),
                    platform=origin.platform,
                    channel_id=origin.parent_channel_id,
                ),
                thread_id=origin.thread_id,
            ),
            account_id=str(auth.account_id),
        )
        async with runtime.session_factory() as db:
            # Admission prefers the shared slot on ever-shared threads. Check
            # both slots: neither history may supply an unsupported enrollment.
            for candidate in (Slot(thread=slot.thread), slot):
                binding = await mux_state.get_binding(db, candidate, read_persisted=True)
                if binding is None:
                    continue
                if (
                    binding.thread != candidate.thread
                    or binding.legacy_account_id != candidate.account_id
                ):
                    raise ToolError("The model provider differs from the owner's selection")
                retained, _, _ = await channel_backend.resolve_turn_backend(
                    db,
                    slot.thread.channel,
                    None,
                    enabled=True,
                    retained_revision=binding.config_revision,
                )
                if binding.config_revision == 0:
                    resolved = resolve_default(BackendConfig())
                else:
                    if (
                        retained is None
                        or retained.channel != slot.thread.channel
                        or retained.local != binding.config_revision
                    ):
                        raise ToolError("The model provider's settings changed; try again")
                    resolved = retained
                if (resolved.backend, resolved.profile) != (binding.provider, binding.profile):
                    raise ToolError("The model provider's settings changed; try again")
                if get_profile(resolved.profile).support_for("tool_confirmation") not in SUPPORTED:
                    raise ToolError("The model provider cannot verify the owner's approval")
        gap = await _session_gate.session_card_gap(runtime, auth, origin, tool_name=tool_name)
    except Exception:
        # Unsupported backends and unreadable proofs cannot authorize enrollment.
        raise ToolError("Owner card approval cannot be verified on this backend/session") from None
    if gap is not None:
        raise ToolError("Owner card approval is required; this backend/session cannot prove it")
    return origin


async def create_watch_impl(
    runtime: McpRuntime,
    auth: AuthIdentity,
    *,
    origin_context_id: str,
    instructions: str,
    source: Literal["page_event", "poll"],
    server: str = DAIMON_SERVER_NAME,
    tool: str = "read_thread",
    arguments: dict[str, object] | None = None,
    projection: str = ".",
    rule: str = "change",
    interval: int = 300,
    lifetime_days: int = 7,
    key: str | None = None,
    evidence: list[str] | None = None,
) -> WatchSummary:
    origin = await require_attended_owner(
        runtime, auth, origin_context_id, tool_name="create_watch"
    )
    if source == "page_event":
        raise ToolError("Page-event watches are unavailable until a page-event producer exists")
    assert auth.platform_user_id is not None
    if not 1 <= lifetime_days <= 30:
        raise ToolError("Watch lifetime must be 1 to 30 days")
    now = datetime.now(UTC)
    binding = ContinuationRequest(
        tenant_id=auth.tenant_id,
        platform=cast(ChatPlatform, auth.platform),
        parent_channel_id=origin.parent_channel_id,
        thread_id=origin.thread_id,
        requester_account_id=auth.account_id,
        requester_external_user_id=auth.platform_user_id,
        target_ma_agent_id=origin.responder_ma_agent_id,
        target_name=origin.responder_name,
        requested_work=None,
        reason="watch_fired",
        idempotency_key=uuid.uuid4(),
    )
    if server != DAIMON_SERVER_NAME:
        return await create_mcp_watch(
            runtime,
            auth,
            binding,
            server=server,
            instructions=instructions,
            tool=tool,
            arguments=arguments or {},
            projection=projection,
            rule=rule,
            interval=interval,
            expires_at=now + timedelta(days=lifetime_days),
            now=now,
            key=key,
            evidence=evidence,
        )
    try:
        async with runtime.session_factory.begin() as db:
            watch = await watches.register_poll_watch(
                db,
                binding,
                instructions=instructions,
                tool=tool,
                arguments=arguments or {},
                projection=projection,
                rule=rule,
                interval=interval,
                expires_at=now + timedelta(days=lifetime_days),
                now=now,
                key=key,
                evidence=evidence,
            )
            # Current owner/agent/seal policy, independently of the origin grant.
            await authorize_poll(runtime, watch)
        return summary(watch)
    except ValueError as exc:
        raise ToolError(str(exc)) from exc


async def create_mcp_watch(
    runtime: McpRuntime,
    auth: AuthIdentity,
    binding: ContinuationRequest,
    *,
    server: str,
    instructions: str,
    tool: str,
    arguments: dict[str, object],
    projection: str,
    rule: str,
    interval: int,
    expires_at: datetime,
    now: datetime,
    key: str | None,
    evidence: list[str] | None,
) -> WatchSummary:
    """A watch on a listed read tool of a server this agent attaches.

    `server` is the server's URL or its name on this agent; the canonical URL is
    what gets stored and checked, never the name. The tool's live listing is read
    with the poll's own credential before anything is saved.
    """
    try:
        agent = await retrieve_agent(
            runtime.client,
            binding.target_ma_agent_id,
            scope=resource_scope(tenant_id=str(auth.tenant_id)),
        )
        if server.lower().startswith("https://"):
            attached = [item for item in agent.mcp_servers if same_ascii_mcp_url(item.url, server)]
        else:
            attached = [item for item in agent.mcp_servers if item.name == server]
        if len(attached) != 1 or attached[0].name == DAIMON_SERVER_NAME:
            raise ValueError("This agent has no such server. Ask me to list its servers.")
        server_url = ascii_mcp_url(attached[0].url)
        listed = require_listed(server_url, tool)
        args = TypeAdapter(dict[str, JsonValue]).validate_python(arguments)
        validate_mcp_poll(
            args,
            projection=projection,
            rule=rule,
            interval=interval,
            key=key,
            evidence=evidence or (),
        )
        server_name = attached_server(agent, server_url, tool)
        hidden = await resolve_hidden_mcp_server_names(
            runtime.session_factory,
            tenant_id=auth.tenant_id,
            agent_id=derive_agent_uuid(
                tenant_id=auth.tenant_id, ma_agent_id=binding.target_ma_agent_id
            ),
            account_id=auth.account_id,
            server_urls={str(item.name): str(item.url) for item in agent.mcp_servers},
        )
        if server_name in hidden:
            raise ValueError(
                "That server needs each person's own sign-in, which watches cannot use yet"
            )
        if operator_denied(runtime.settings.tool_safety, server_name=server_name, tool=tool):
            raise ValueError("Whoever runs this Daimon has blocked that tool")
        try:
            token = await poll_credential(
                runtime,
                tenant_id=auth.tenant_id,
                agent_id=binding.target_ma_agent_id,
                owner_account_id=auth.account_id,
                server_url=server_url,
                credential=listed.credential,
            )
        except CredentialMissing:
            raise ToolError(
                "This tool reads with your own key, and no read-only key of yours is saved. "
                "Call request_watch_credential for this server so they can send it privately, "
                "then make the watch again. Never ask for the key in chat."
            ) from None
        try:
            # The same overall bound a poll has, so a slow server cannot hold the turn.
            async with (
                asyncio.timeout(25),
                polling.open_source(server_url, token) as source,
            ):
                entry = await listed_read_tool(
                    runtime, source, server_name=server_name, tool=tool, arguments=args
                )
                digest = catalogue_digest(entry)
        except (McpCallError, TimeoutError, RecursionError):
            raise ValueError("Could not read that server's tool list. Try again later.") from None
        async with runtime.session_factory.begin() as db:
            watch = await watches.register_mcp_poll_watch(
                db,
                binding,
                instructions=instructions,
                server_url=server_url,
                server_name=server_name,
                tool=tool,
                arguments=dict(args),
                projection=projection,
                rule=rule,
                interval=interval,
                catalogue_digest=digest,
                expires_at=expires_at,
                now=now,
                key=key,
                evidence=evidence,
            )
            # Current owner/agent/seal policy, independently of the origin grant.
            await authorize_poll(runtime, watch)
        return summary(watch)
    except PollAccessRevoked:
        raise ToolError(
            "This agent cannot use that tool for a watch. Ask an admin or an owner."
        ) from None
    except ValueError as exc:
        raise ToolError(str(exc)) from exc


async def rotate_watch_impl(
    runtime: McpRuntime, auth: AuthIdentity, *, watch_id: str, origin_context_id: str | None = None
) -> WatchSummary:
    try:
        watch = await enrollment_preflight(
            runtime, auth, watch_id, origin_context_id, tool_name="rotate_watch_key"
        )
        if watch.source != "page_event":
            raise ValueError("Poll watches have no signing key")
        key_id, key = secrets.token_hex(12), secrets.token_urlsafe(48)
        async with runtime.session_factory.begin() as db:
            current = await watches.require_owned_watch(
                db,
                uuid.UUID(watch_id),
                tenant_id=auth.tenant_id,
                account_id=auth.account_id,
                is_admin=False,
            )
            require_unchanged_enrollment(watch, current)
            await watches.rotate_signing_key(
                db, watch.id, key_id=key_id, key=key, expires_at=watch.expires_at
            )
        return await deliver_rotated_key(runtime, auth, watch, key_id=key_id, key=key)
    except ValueError as exc:
        raise ToolError(str(exc)) from exc


async def enrollment_preflight(
    runtime: McpRuntime,
    auth: AuthIdentity,
    watch_id: str,
    origin_context_id: str | None,
    *,
    tool_name: str,
) -> WatchRow:
    """Do provider I/O before acquiring the global firing/control lock."""
    async with runtime.session_factory() as db:
        row = await watches.get_watch(db, uuid.UUID(watch_id))
    if row is None or row.tenant_id != auth.tenant_id or row.owner_account_id != auth.account_id:
        raise ValueError("Watch not found")
    await require_attended_owner(runtime, auth, origin_context_id, tool_name=tool_name)
    await authorize_poll(runtime, row)
    return row


def require_unchanged_enrollment(before: WatchRow, current: WatchRow) -> None:
    # Poll bookkeeping may advance during I/O; binding, control state and
    # enrollment parameters must still be exactly those that were authorized.
    changing = {
        "baseline",
        "fire_count",
        "lease_owner",
        "lease_expires_at",
        "next_check_at",
        "backoff_until",
        "consecutive_errors",
    }
    if before.model_dump(exclude=changing) != current.model_dump(exclude=changing):
        raise ValueError("Watch changed during approval; retry enrollment")


async def deliver_rotated_key(
    runtime: McpRuntime, auth: AuthIdentity, watch: WatchRow, *, key_id: str, key: str
) -> WatchSummary:
    """Deliver an already-authorized key; failure pauses without exposing its value."""
    try:
        await private_key_delivery(runtime, auth, watch, key_id=key_id, key=key)
    except Exception:
        async with runtime.session_factory.begin() as db:
            await watches.pause_watch(db, watch.id, reason="key_delivery_failed")
        raise ToolError(
            "Private key delivery failed; watch paused. Rotate again to retry."
        ) from None
    return summary(watch, key_id=key_id)


async def manage_watch_impl(
    runtime: McpRuntime,
    auth: AuthIdentity,
    *,
    watch_id: str,
    action: Literal["show", "pause", "resume", "delete"],
    origin_context_id: str | None = None,
) -> WatchSummary:
    try:
        admin = await fresh_admin(runtime, auth)
        approved = None
        if action == "resume":
            approved = await enrollment_preflight(
                runtime, auth, watch_id, origin_context_id, tool_name="resume_watch"
            )
        async with runtime.session_factory.begin() as db:
            row = await watches.require_owned_watch(
                db,
                uuid.UUID(watch_id),
                tenant_id=auth.tenant_id,
                account_id=auth.account_id,
                is_admin=admin and action != "resume",
            )
            now = datetime.now(UTC)
            if action == "pause":
                await watches.pause_watch(db, row.id)
            elif action == "resume":
                assert approved is not None
                require_unchanged_enrollment(approved, row)
                await watches.resume_watch(db, row.id, now=now)
            elif action == "delete":
                await watches.delete_watch(db, row.id, now=now)
                return summary(row.model_copy(update={"status": "deleted"}))
            updated = await watches.get_watch(db, row.id)
            assert updated is not None
            return summary(updated)
    except ValueError as exc:
        raise ToolError(str(exc)) from exc


def register_watch_tools(mcp: FastMCP, runtime: McpRuntime) -> None:
    @mcp.tool(tags={"discord"})
    async def create_watch(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        origin_context_id: str,
        instructions: str,
        source: Literal["poll"],
        server: str = DAIMON_SERVER_NAME,
        tool: str = "read_thread",
        arguments: dict[str, object] | None = None,
        projection: str = ".",
        rule: Literal["change", "added", "updated"] = "change",
        interval: int = 300,
        lifetime_days: int = 7,
        key: str | None = None,
        evidence: list[str] | None = None,
    ) -> WatchSummary:
        """Watch a listed read tool from this conversation: a Discord reader or an MCP server's.

        Binds the current human owner, tenant, agent and exact Discord thread.
        Requires an attended turn origin and verified owner card approval.
        Backends without tool confirmation refuse enrollment.
        Polls run without an AI turn; firings
        use unattended read-and-report turns and current access/spend checks.
        Only tools on this deployment's watch list (set by whoever runs Daimon) can be
        checked; by default that is read_thread and read_channel on daimon-mcp. An
        unlisted tool is refused, and a watch whose tool is taken off the list pauses
        and its owner is told. For read_thread and read_channel the arguments must name
        a source different from the reporting thread.
        `server` is "daimon-mcp" (the default) or, for a tool on another MCP server this
        agent attaches, that server's https URL; pass the URL so the approval card shows
        its host. A server name on this agent is also accepted and resolved to its URL,
        which is what is stored. Such a tool must be listed for that URL, enabled on
        this agent, and its server must say it only reads (readOnlyHint true, not
        destructive); that hint can only refuse, never allow. Its listing is read when
        the watch is made and on every poll, and a change pauses the watch. Servers
        that need each person's own browser sign-in are refused. A tool listed to read
        with the owner's own key needs that key first: if none is saved, call
        request_watch_credential so they send it privately, never in chat.
        Projection supports `.`, `.field.nested`, `.rows[0].field` and one `[]` step
        such as `.rows[].content`; other jq expressions are refused. Polls start with a
        saved baseline and compare change, added array elements, or updated object keys,
        no more often than the tool's listed shortest interval (at most every 86400 s).
        With rule `added`, `key` (such as `.rows[].id`) compares items by that field and
        keeps only short fingerprints of the keys, and `evidence` lists up to 8 fields
        (such as `.content`) to report for each new item. One report holds at most 20
        items and counts the rest as omitted.
        Lifetime is 1–30 days. Producers remain disabled until both operator gates
        are enabled. Page-event watches are refused until a producer exists.
        """
        return await create_watch_impl(
            runtime,
            await _auth(ctx),
            origin_context_id=origin_context_id,
            instructions=instructions,
            source=source,
            server=server,
            tool=tool,
            arguments=arguments,
            projection=projection,
            rule=rule,
            interval=interval,
            lifetime_days=lifetime_days,
            key=key,
            evidence=evidence,
        )

    @mcp.tool(tags={"discord"})
    async def list_watches(  # pyright: ignore[reportUnusedFunction]
        ctx: Context, scope: Literal["mine", "all"] = "mine"
    ) -> list[WatchListEntry]:
        """List your watches in this workspace; admins may list every owner's with scope "all".

        Includes paused imports and expiry. `scope="all"` stays within this workspace and
        rechecks admin live, like admin show, pause and delete; anyone else is refused. Each entry
        names its owner, status and pause reason, source and destination kind, expiry, fires
        in the past day, last firing and consecutive errors. Never instructions, signing
        secrets or source evidence.
        """
        return await list_watches_impl(runtime, await _auth(ctx), scope=scope)

    @mcp.tool(tags={"discord"})
    async def show_watch(ctx: Context, watch_id: str) -> WatchSummary:  # pyright: ignore[reportUnusedFunction]
        """Show your watch's immutable destination, source, status and expiry. Admins may inspect
        within their workspace.
        """
        return await manage_watch_impl(runtime, await _auth(ctx), watch_id=watch_id, action="show")

    @mcp.tool(tags={"discord"})
    async def pause_watch(ctx: Context, watch_id: str) -> WatchSummary:  # pyright: ignore[reportUnusedFunction]
        """Pause your watch and cancel its queued wakes. Workspace admins may pause another
        owner's watch.
        """
        return await manage_watch_impl(runtime, await _auth(ctx), watch_id=watch_id, action="pause")

    @mcp.tool(tags={"discord"})
    async def resume_watch(ctx: Context, watch_id: str, origin_context_id: str) -> WatchSummary:  # pyright: ignore[reportUnusedFunction]
        """Resume your paused watch from an attended owner turn after fresh access checks.
        Only the owner may resume; an admin cannot override the owner pause.
        Server-side owner card proof is required, including with tool safety off.
        Backends without tool confirmation refuse.
        Imported watches require operator confirmation; source_unmapped never resumes.
        """
        return await manage_watch_impl(
            runtime,
            await _auth(ctx),
            watch_id=watch_id,
            action="resume",
            origin_context_id=origin_context_id,
        )

    @mcp.tool(tags={"discord"})
    async def rotate_watch_key(ctx: Context, watch_id: str, origin_context_id: str) -> WatchSummary:  # pyright: ignore[reportUnusedFunction]
        """Rotate a page signing key, revoke the old key and cancel queued wakes. The fresh
        key goes to its owner by private DM once. Requires an attended owner turn;
        an admin cannot rotate another owner's key. Server-side owner card proof is
        required; backends without tool confirmation refuse.
        """
        return await rotate_watch_impl(
            runtime, await _auth(ctx), watch_id=watch_id, origin_context_id=origin_context_id
        )

    @mcp.tool(tags={"discord"})
    async def delete_watch(ctx: Context, watch_id: str) -> WatchSummary:  # pyright: ignore[reportUnusedFunction]
        """Delete your watch and its retained evidence after cancelling queued wakes; admins may
        delete within their workspace.
        """
        return await manage_watch_impl(
            runtime, await _auth(ctx), watch_id=watch_id, action="delete"
        )
