"""Trusted watch executor for sidecar leases, using existing owner-authorized reads.

The lease is an opaque short-lived scheduling capability, not a caller-selected
identity. No source data or tool arguments cross the sidecar transport. Model
clients are used only to retrieve current agent facts; no AI turn is started.
"""

from __future__ import annotations

import asyncio
import random
import secrets
import time
import uuid
from collections import deque
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import discord
import structlog
from anthropic import NotFoundError, PermissionDeniedError
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools._channel_policy import ChannelReadPolicy
from daimon.adapters.mcp.tools.discord._client import (
    _require_bot_token,  # pyright: ignore[reportPrivateUsage]
    _require_guild_channel,  # pyright: ignore[reportPrivateUsage]
    _resolve_channel,  # pyright: ignore[reportPrivateUsage]
    _resolve_member,  # pyright: ignore[reportPrivateUsage]
    rest_client,
)
from daimon.adapters.mcp.tools.discord._read import (
    _read_channel_impl,  # pyright: ignore[reportPrivateUsage]
    _read_thread_impl,  # pyright: ignore[reportPrivateUsage]
)
from daimon.adapters.mcp.tools.discord._visibility import (
    _check_thread_view,  # pyright: ignore[reportPrivateUsage]
    _check_view_permission,  # pyright: ignore[reportPrivateUsage]
    _ensure_thread_parent_cached,  # pyright: ignore[reportPrivateUsage]
)
from daimon.core.agent_mcp_credentials import resolve_hidden_mcp_server_names
from daimon.core.artifact_pages.documents import build_list_query
from daimon.core.artifact_pages.standing import visitor_accounts
from daimon.core.authz import Action, Place, Surface, authorize, build_agent_ref
from daimon.core.bot_access import BotAccessUnavailable, bot_access_wait_on, is_missing_access
from daimon.core.config import load_watches_settings
from daimon.core.continuity.poll_tools import (
    PAGE_DATA_TOOL,
    SourceChanged,
    ToolUnlisted,
    catalogue_digest,
    require_distinct_source,
    require_listed,
    require_read_only_listing,
    require_schema_arguments,
    validate_mcp_poll,
    validate_poll,
    watch_server,
)
from daimon.core.continuity.watch_projection import (
    BASELINE,
    SOURCE,
    Rule,
    bounded,
    compare,
    kept,
    project,
)
from daimon.core.continuity.watch_projection import (
    payload as evidence_payload,
)
from daimon.core.errors import WatchFeatureUnreadable
from daimon.core.github_credentials import decrypt_token
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.mcp_auth import mint_chat_mcp_token
from daimon.core.mcp_identity import RelayBinding, relay_server_name
from daimon.core.mcp_server_url import ascii_mcp_url, same_ascii_mcp_url
from daimon.core.mcp_tool_annotations import annotations_from_mcp
from daimon.core.mcp_tool_call import McpToolSession, mcp_tool_session
from daimon.core.membership_subject import resolve_subject
from daimon.core.mux_backend import resource_scope
from daimon.core.mux_compat import retrieve_agent
from daimon.core.ops_catalog import OpsCode
from daimon.core.ops_events import OpsEvent, report_ops_event
from daimon.core.stores import agent_mcp_credentials as credential_store
from daimon.core.stores import artifact_pages, web_people
from daimon.core.stores.access_policy import AccessPolicyUnreadable, load_access_policy
from daimon.core.stores.accounts import get_account
from daimon.core.stores.direct_messages import is_dm_thread
from daimon.core.stores.domain import WatchRow
from daimon.core.stores.tenants import get_tenant
from daimon.core.stores.watch_credentials import load_watch_credential
from daimon.core.stores.watches import (
    DailyCap,
    PollBindingRevoked,
    defer_busy_poll,
    finish_poll,
    hold_to_floor,
    leased_poll,
    pause_unlisted,
    poll_failed,
    release_poll_lease,
)
from daimon.core.tool_safety import (
    DAIMON_SERVER_NAME,
    ToolAnnotations,
    ToolCall,
    ToolSafetyPolicy,
    classify_tool,
)
from daimon.core.turn.approvals import agent_rule_lookup
from daimon.core.turn_hold import TurnHold
from pydantic import JsonValue, TypeAdapter
from sqlalchemy.exc import DBAPIError
from starlette.requests import Request
from starlette.responses import Response


class PollAccessRevoked(ValueError):
    pass


class CredentialMissing(Exception):
    """The owner's own key for this server is gone; pauses as `credential_missing`."""


class SourceBusy(Exception):
    """This server's own poll budget is spent; retry at the next claim, not an error."""


class UrlBudget:
    """At most `concurrent` polls at once and `per_minute` a minute per server URL,
    so one slow server cannot use up the executor's shared budget."""

    def __init__(self, *, concurrent: int = 2, per_minute: int = 30) -> None:
        self.concurrent = concurrent
        self.per_minute = per_minute
        self._active: dict[str, int] = {}
        self._recent: dict[str, deque[float]] = {}

    @contextmanager
    def take(self, url: str) -> Iterator[None]:
        now = time.monotonic()
        recent = self._recent.setdefault(url, deque())
        while recent and recent[0] <= now - 60:
            recent.popleft()
        if self._active.get(url, 0) >= self.concurrent or len(recent) >= self.per_minute:
            raise SourceBusy
        recent.append(now)
        self._active[url] = self._active.get(url, 0) + 1
        try:
            yield
        finally:
            self._active[url] -= 1
            if not self._active[url]:
                del self._active[url]
            if not recent:
                self._recent.pop(url, None)


SOURCE_BUDGET = UrlBudget()

#: How far a watch whose server is busy steps back in the claim queue (plus jitter).
BUSY_DEFER_S = 60
OFF_DEFER_S = 600
"""How long a watch that may not run now (Off here, expired) waits before it is claimed again."""

#: Opens the bounded MCP session for one poll; tests swap in a fake server.
open_source = mcp_tool_session
#: A relay poll's own credential outlives the poll's 25-second bound, and no more.
RELAY_POLL_TTL = timedelta(minutes=2)


def operator_denied(policy: ToolSafetyPolicy, *, server_name: str, tool: str) -> bool:
    """A `denied` entry, or an `effects` pin that makes the tool a write, refuses the poll."""
    if f"{server_name}/{tool}" in policy.denied or server_name in policy.denied:
        return True
    # With the server's own read-only hint assumed, only an operator pin can say write.
    hinted = ToolAnnotations(read_only_hint=True, destructive_hint=False)
    return (
        classify_tool(policy, server_name=server_name, tool_name=tool, annotations=hinted) != "read"
    )


def attached_server(agent: Any, server_url: str, tool: str) -> str:  # noqa: ANN401 - MA agent
    """The agent still attaches exactly one server at this canonical URL, whatever its
    name, with the tool enabled. Returns that server's current name."""
    servers = [server for server in agent.mcp_servers if same_ascii_mcp_url(server.url, server_url)]
    if len(servers) != 1 or servers[0].name == DAIMON_SERVER_NAME:
        raise PollAccessRevoked("Agent no longer attaches the watched server")
    name = str(servers[0].name)
    toolsets = [
        entry
        for entry in agent.tools
        if entry.type == "mcp_toolset" and entry.mcp_server_name == name
    ]
    if len(toolsets) != 1:
        raise PollAccessRevoked("Agent no longer attaches the watched server")
    configs = [config for config in toolsets[0].configs if config.name == tool]
    config = configs[0] if len(configs) == 1 else toolsets[0].default_config
    if len(configs) > 1 or config is None or not config.enabled:
        raise PollAccessRevoked("Agent poll tool is disabled")
    return name


async def poll_credential(
    runtime: McpRuntime,
    *,
    tenant_id: uuid.UUID,
    agent_id: str,
    owner_account_id: uuid.UUID,
    server_url: str,
    credential: str,
    relay: RelayBinding | None = None,
    hold: TurnHold | None = None,
) -> str | None:
    """Decrypt exactly one credential for this exact URL: the agent-wide one, the
    watch owner's own key (looked up by tenant and owner, never another owner's), or none.

    Credential "relay" instead mints the owner a short-lived credential for this
    Daimon's own relay URL, signing the `relay` binding (the watch it polls for, if
    any), so the relay names the owner as an unattended caller and the watch's place.
    """
    if credential == "none":
        return None
    if credential == "relay":
        mcp = runtime.settings.mcp
        if (
            relay is None
            or mcp.public_url is None
            or mcp.jwt_secret is None
            or relay_server_name(server_url, str(mcp.public_url)) is None
        ):
            raise PollAccessRevoked("Watch credential unavailable")
        async with runtime.session_factory.begin() as db:
            token, _, _ = await mint_chat_mcp_token(
                db,
                account_id=owner_account_id,
                agent_id=derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=agent_id),
                secret=mcp.jwt_secret.get_secret_value().encode(),
                now=datetime.now(UTC),
                relay=relay,
                hold=hold,
                ttl=RELAY_POLL_TTL,
            )
        return token
    if credential not in ("agent", "owner") or runtime.fernet is None:
        raise PollAccessRevoked("Watch credential unavailable")
    if credential == "owner":
        async with runtime.session_factory.begin() as db:
            sealed = await load_watch_credential(
                db,
                tenant_id=tenant_id,
                owner_account_id=owner_account_id,
                server_url=server_url,
                now=datetime.now(UTC),
            )
        if sealed is None:
            raise CredentialMissing
        return decrypt_token(runtime.fernet, sealed)
    async with runtime.session_factory() as db:
        rows = await credential_store.list_credentials(
            db,
            tenant_id=tenant_id,
            agent_id=derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=agent_id),
        )
    matches = [row for row in rows if same_ascii_mcp_url(row.mcp_server_url, server_url)]
    if len(matches) != 1:
        raise PollAccessRevoked("Watch credential unavailable")
    return decrypt_token(runtime.fernet, matches[0].encrypted_token)


# The exact messages the firing SQL raises with SQLSTATE 54000 for each daily cap
# (alembic 0143). Rate and quota limits share the SQLSTATE and stay silent.
DAILY_CAPS: dict[str, DailyCap] = {"watch daily cap": "watch", "owner daily cap": "owner"}


def daily_cap(exc: DBAPIError) -> DailyCap | None:
    """Which daily cap the firing SQL refused with, from its primary message."""
    orig = exc.orig
    diag = getattr(orig, "diag", None)  # psycopg
    # asyncpg's own error, which SQLAlchemy's adapter chains as the cause.
    message = getattr(diag, "message_primary", None) or getattr(
        getattr(orig, "__cause__", None), "message", None
    )
    return DAILY_CAPS.get(message) if isinstance(message, str) else None


async def poll_read_place(
    runtime: McpRuntime,
    auth: AuthIdentity,
    resource_id: str,
    *,
    destination: bool = False,
) -> Place:
    """Fetch native ACLs as the owner, including history and private-thread membership."""
    if auth.external_id is None or auth.platform_user_id is None:
        raise PollAccessRevoked("Missing owner identity")
    async with rest_client(_require_bot_token(runtime)) as client:
        _, member = await _resolve_member(client, auth.external_id, auth.platform_user_id)
        try:
            resolved = await _resolve_channel(client, resource_id)
        except discord.Forbidden as err:
            # The bot's own read of the channel. A missing channel keeps its old handling.
            if is_missing_access(err.status, err.code) and await bot_access_wait_on(
                runtime.session_factory, tenant_id=auth.tenant_id, platform="discord"
            ):
                raise BotAccessUnavailable from None
            raise
        channel = _require_guild_channel(resolved, auth.external_id)
        if isinstance(channel, discord.Thread):
            await _check_thread_view(client, channel, member, auth.platform_user_id)
            parent = await _ensure_thread_parent_cached(channel)
            place = Place(
                channel_id=str(channel.id),
                parent_channel_id=str(parent.id),
                category_id=str(parent.category_id) if parent.category_id else None,
            )
        else:
            _check_view_permission(channel, member)
            place = Place(
                channel_id=str(channel.id),
                category_id=str(channel.category_id) if channel.category_id else None,
            )
        permissions = channel.permissions_for(member)
        if not permissions.view_channel or not permissions.read_message_history:
            raise PollAccessRevoked("Owner cannot read source history")
        if destination and (
            not isinstance(channel, discord.Thread) or not permissions.send_messages_in_threads
        ):
            raise PollAccessRevoked("Owner cannot use reporting thread")
        return place


async def authorize_poll(
    runtime: McpRuntime,
    watch: WatchRow,
) -> tuple[AuthIdentity, ChannelReadPolicy]:
    auth, read, _, _ = await authorize_source(runtime, watch)
    return auth, read


async def authorize_source(
    runtime: McpRuntime,
    watch: WatchRow,
) -> tuple[AuthIdentity, ChannelReadPolicy, str | None, str]:
    """Recheck access for every poll, before and after the read. Also returns the name
    the agent attaches an MCP source under now (None for a native source) and the
    agent's own name now."""
    server_name: str | None = None
    if watch.source in ("poll", "mcp_poll"):
        # The operator registry, read fresh each time: before and after every read.
        listed = require_listed(
            watch_server(watch.source, watch.source_state), str(watch.source_state.get("tool"))
        )
        if watch.source == "mcp_poll" and (
            listed.credential != watch.source_state.get("credential")
        ):
            raise PollAccessRevoked("Watch credential changed")
    if watch.platform == "web":
        return await _authorize_web_poll(runtime, watch)
    async with runtime.session_factory() as db:
        tenant = await get_tenant(db, watch.tenant_id)
        account = await get_account(db, watch.owner_account_id)
    if (
        tenant is None
        or account is None
        or account.tenant_id != watch.tenant_id
        or runtime.membership_resolver is None
    ):
        raise PollAccessRevoked("Watch owner unavailable")
    subject = await resolve_subject(
        runtime.session_factory,
        runtime.membership_resolver,
        tenant_id=watch.tenant_id,
        user_id=watch.owner_external_id,
        require_fresh=True,
    )
    if subject.verified_membership is not True:
        raise PollAccessRevoked("Watch membership revoked")
    agent = await retrieve_agent(
        runtime.client,
        watch.agent_id,
        scope=resource_scope(tenant_id=str(watch.tenant_id)),
        records=runtime.session_factory,
    )
    ref = build_agent_ref(agent.name, agent.metadata)
    auth = AuthIdentity(
        account_id=watch.owner_account_id,
        tenant_id=watch.tenant_id,
        role=account.role,
        platform=watch.platform,
        external_id=tenant.external_id,
        platform_user_id=watch.owner_external_id,
        chat_agent_id=derive_agent_uuid(tenant_id=watch.tenant_id, ma_agent_id=watch.agent_id),
    )
    place = await poll_read_place(runtime, auth, watch.thread_id, destination=True)
    if place.parent_channel_id != watch.parent_channel_id:
        raise PollAccessRevoked("Watch destination changed")
    source_place: Place | None = None
    if watch.source == "poll":
        public_url = runtime.settings.mcp.public_url
        if public_url is None:
            raise PollAccessRevoked("Daimon MCP endpoint unavailable")
        tool = str(watch.source_state.get("tool"))
        servers = [server for server in agent.mcp_servers if server.name == DAIMON_SERVER_NAME]
        toolsets = [
            entry
            for entry in agent.tools
            if entry.type == "mcp_toolset" and entry.mcp_server_name == DAIMON_SERVER_NAME
        ]
        if (
            len(servers) != 1
            or servers[0].url.rstrip("/") != str(public_url).rstrip("/")
            or len(toolsets) != 1
        ):
            raise PollAccessRevoked("Agent MCP toolset unavailable")
        toolset = toolsets[0]
        configs = [config for config in toolset.configs if config.name == tool]
        config = configs[0] if len(configs) == 1 else toolset.default_config
        if (
            len(configs) > 1
            or not config.enabled
            or config.permission_policy.type != "always_allow"
        ):
            raise PollAccessRevoked("Agent poll tool is disabled or requires approval")
        rules = agent_rule_lookup(
            runtime.session_factory,
            tenant_id=watch.tenant_id,
            agent_id=watch.agent_id,
            channel_id=watch.parent_channel_id,
            server_urls={DAIMON_SERVER_NAME: str(public_url)},
            also_urls={DAIMON_SERVER_NAME: str(public_url)},
        )
        current = await rules(
            ToolCall(tool_use_id="poll", server_name=DAIMON_SERVER_NAME, tool_name=tool, input={})
        )
        if current is not None and current.rule != "allow":
            raise PollAccessRevoked("Poll tool requires approval or is denied")
        arguments = cast(dict[str, object], watch.source_state.get("arguments", {}))
        if tool != PAGE_DATA_TOOL:
            # A page's data has no chat place; its reader checks the owner's page access.
            source_id = str(arguments.get("thread_id", arguments.get("channel_id", "")))
            try:
                require_distinct_source(arguments, watch.thread_id)
            except ValueError as exc:
                raise PollAccessRevoked(str(exc)) from exc
            source_place = await poll_read_place(runtime, auth, source_id)
            # Identity equality follows native resolution, never caller spelling.
            if source_place.channel_id == place.channel_id:
                raise PollAccessRevoked("Poll source resolves to the reporting thread")
    elif watch.source == "mcp_poll":
        server_url = str(watch.source_state["server_url"])
        tool = str(watch.source_state.get("tool"))
        server_name = attached_server(agent, server_url, tool)
        server_urls = {str(server.name): str(server.url) for server in agent.mcp_servers}
        hidden = await resolve_hidden_mcp_server_names(
            runtime.session_factory,
            tenant_id=watch.tenant_id,
            agent_id=derive_agent_uuid(tenant_id=watch.tenant_id, ma_agent_id=watch.agent_id),
            account_id=watch.owner_account_id,
            server_urls=server_urls,
        )
        if server_name in hidden:
            raise PollAccessRevoked("Watched server needs the owner's own sign-in")
        if operator_denied(runtime.settings.tool_safety, server_name=server_name, tool=tool):
            raise PollAccessRevoked("Poll tool is denied or pinned as a write")
        rules = agent_rule_lookup(
            runtime.session_factory,
            tenant_id=watch.tenant_id,
            agent_id=watch.agent_id,
            channel_id=watch.parent_channel_id,
            server_urls={server_name: server_urls[server_name]},
        )
        current = await rules(
            ToolCall(tool_use_id="poll", server_name=server_name, tool_name=tool, input={})
        )
        if current is not None and current.rule != "allow":
            raise PollAccessRevoked("Poll tool requires approval or is denied")
    # Policy is read after all native/provider lookups, immediately before the
    # source call. The executor repeats this check after the read before commit.
    async with runtime.session_factory() as db:
        policy = await load_access_policy(db, tenant_id=watch.tenant_id)
    for action in (Action.START_TURN, Action.RUN_AGENT):
        if not authorize(
            policy,
            subject=subject,
            action=action,
            # Polls have a recorded owner but no human invoking the agent.
            surface=Surface.ROUTINE,
            agent=ref,
            place=place,
        ):
            raise PollAccessRevoked("Watch access revoked")
    read = ChannelReadPolicy(
        policy, frozenset({watch.parent_channel_id, watch.thread_id}), ref, origin_place=place
    )
    read.require(watch.thread_id, watch.parent_channel_id)
    if source_place is not None and not authorize(
        policy,
        subject=subject,
        action=Action.READ_CHANNEL,
        agent=ref,
        place=source_place,
        origin=place,
        origin_channel_ids=frozenset({watch.parent_channel_id, watch.thread_id}),
    ):
        raise PollAccessRevoked("Poll source is sealed")
    return auth, read, server_name, str(agent.name)


async def _authorize_web_poll(
    runtime: McpRuntime, watch: WatchRow
) -> tuple[AuthIdentity, ChannelReadPolicy, str | None, str]:
    """A web watch polls only a page's data, as its owner: a member still in the workspace,
    not a visitor or an outsider. The read itself checks they can still use the page
    (`read_page_data`)."""
    if watch.source != "poll" or watch.source_state.get("tool") != PAGE_DATA_TOOL:
        raise PollAccessRevoked("Web watches poll only page data")
    async with runtime.session_factory() as db:
        tenant = await get_tenant(db, watch.tenant_id)
        account = await get_account(db, watch.owner_account_id)
        person = await web_people.get_person_for_account(
            db, tenant_id=watch.tenant_id, account_id=watch.owner_account_id
        )
        policy = await load_access_policy(db, tenant_id=watch.tenant_id)
        visitor = await visitor_accounts(
            db, tenant_id=watch.tenant_id, account_ids=[watch.owner_account_id]
        )
    if (
        tenant is None
        or account is None
        or account.tenant_id != watch.tenant_id
        or person is None
        or person.status != "active"
        or visitor
    ):
        raise PollAccessRevoked("Watch owner unavailable")
    agent = await retrieve_agent(
        runtime.client,
        watch.agent_id,
        scope=resource_scope(tenant_id=str(watch.tenant_id)),
        records=runtime.session_factory,
    )
    auth = AuthIdentity(
        account_id=watch.owner_account_id,
        tenant_id=watch.tenant_id,
        role=account.role,
        platform=watch.principal_platform or watch.platform,
        external_id=tenant.external_id,
        platform_user_id=watch.owner_external_id,
        chat_agent_id=derive_agent_uuid(tenant_id=watch.tenant_id, ma_agent_id=watch.agent_id),
    )
    return auth, ChannelReadPolicy(policy), None, str(agent.name)


async def read_page_data(runtime: McpRuntime, watch: WatchRow) -> JsonValue:
    """`artifact_db_query` as the watch owner: refused once they can no longer use the page."""
    arguments = cast(dict[str, JsonValue], watch.source_state["arguments"])
    limits = runtime.settings.artifact_pages
    order_by = cast(str | None, arguments.get("order_by"))
    query = build_list_query(
        str(arguments["collection"]),
        order_by=order_by,
        descending=arguments.get("descending") is True,
        limit=min(int(str(arguments.get("limit", 20))), limits.list_documents),
        by_creation=order_by is None,
        limits=limits,
    )
    async with runtime.session_factory() as db:
        page = await artifact_pages.get_by_slug(
            db, tenant_id=watch.tenant_id, slug=str(arguments["slug"])
        )
        role = (
            "none"
            if page is None
            else await artifact_pages.tool_role(db, page, watch.owner_account_id, chat_admin=False)
        )
        if page is None or role == "none":
            raise PollAccessRevoked("Watch owner can no longer use the page")
        rule = artifact_pages.bound_rule(page, query.collection, role)
        if rule is not None and rule.read == "own":
            query = query.model_copy(update={"created_by": watch.owner_account_id})
        rows = await artifact_pages.list_docs(db, page.id, query, limits=limits)
    return {"rows": [{"id": row.doc_id, "data": row.data} for row in rows]}


async def read_native(
    runtime: McpRuntime, auth: AuthIdentity, read: ChannelReadPolicy, watch: WatchRow
) -> JsonValue:
    """A reviewed reader, as the owner; a chat reader keeps only human-authored rows."""
    if watch.source_state["tool"] == PAGE_DATA_TOOL:
        return await read_page_data(runtime, watch)
    arguments = cast(dict[str, JsonValue], watch.source_state["arguments"])
    limit = int(str(arguments.get("limit", 10)))
    if watch.source_state["tool"] == "read_thread":
        document = await _read_thread_impl(
            runtime,
            auth,
            thread_id=str(arguments["thread_id"]),
            limit=limit,
            read_policy=read,
            poll_humans_only=True,
        )
    else:
        document = await _read_channel_impl(
            runtime,
            auth,
            channel_id=str(arguments["channel_id"]),
            limit=limit,
            read_policy=read,
            poll_humans_only=True,
        )
    # Keep only human-authored rows; no pagination metadata or assistant
    # output can enter any projection, including identity (".").
    raw = document.model_dump(mode="json")
    return {"rows": [row for row in raw["rows"] if row.get("role") == "user"]}


async def read_mcp(
    runtime: McpRuntime, watch: WatchRow, *, server_name: str, credential: str, agent_name: str
) -> JsonValue:
    """One `tools/list` and one `tools/call` on the exact registered URL.

    The listing is read with the poll's own credential. It must still match what
    the owner approved and still say the tool only reads; the hint can only
    refuse. The token stays in this frame and is sent only by `open_source`.
    """
    server_url = str(watch.source_state["server_url"])
    tool = str(watch.source_state["tool"])
    arguments = TypeAdapter(dict[str, JsonValue]).validate_python(watch.source_state["arguments"])
    # A poll reports where the watch does, so a contained room's audience rule binds it
    # as it binds a turn there; a DM has no audience.
    hold = (
        None
        if watch.delivery == "dm" or is_dm_thread(watch.platform, watch.thread_id)
        else TurnHold(watch.platform, watch.parent_channel_id, client=False, relay=True)
    )
    with SOURCE_BUDGET.take(ascii_mcp_url(server_url)):
        token = await poll_credential(
            runtime,
            tenant_id=watch.tenant_id,
            agent_id=watch.agent_id,
            owner_account_id=watch.owner_account_id,
            server_url=server_url,
            credential=credential,
            relay=RelayBinding(agent_name, watch_id=watch.id, owner=watch.owner_external_id),
            hold=hold,
        )
        async with open_source(ascii_mcp_url(server_url), token) as source:
            entry = await listed_read_tool(
                runtime, source, server_name=server_name, tool=tool, arguments=arguments
            )
            if catalogue_digest(entry) != watch.source_state.get("catalogue_digest"):
                raise SourceChanged("The watched tool changed after approval")
            return await source.call_tool(tool, arguments)


async def listed_read_tool(
    runtime: McpRuntime,
    source: McpToolSession,
    *,
    server_name: str,
    tool: str,
    arguments: dict[str, JsonValue],
) -> dict[str, JsonValue]:
    """The tool's live listing, which must say it only reads and fit the arguments."""
    listed = [entry for entry in await source.list_tools() if entry.get("name") == tool]
    if len(listed) != 1:
        raise SourceChanged("That tool is not on its server's list")
    entry = listed[0]
    require_read_only_listing(entry)
    if (
        classify_tool(
            runtime.settings.tool_safety,
            server_name=server_name,
            tool_name=tool,
            annotations=annotations_from_mcp(entry),
        )
        != "read"
    ):
        raise SourceChanged("That tool's server does not say it only reads")
    require_schema_arguments(entry, arguments)
    return entry


log = structlog.get_logger(__name__)


def _log_poll_failed(
    runtime: McpRuntime,
    watch_id: uuid.UUID,
    watch: WatchRow | None,
    *,
    kind: str,
    benign: bool = False,
    error_class: str | None = None,
    consecutive_errors: int | None = None,
) -> None:
    """Fixed fields only: the watch's source, arguments and exception text stay out.

    A `benign` failure (a busy source, the daily cap) is logged at info and records no issue."""
    (log.info if benign else log.warning)(
        "watch.poll_failed",
        tenant_id=str(watch.tenant_id) if watch is not None else None,
        watch_id=str(watch_id),
        kind=kind,
        error_class=error_class,
        consecutive_errors=consecutive_errors,
    )
    if not benign and watch is not None:
        _report_watch(runtime, "watch.poll_failed", watch, error_class or kind)


def _report_watch(runtime: McpRuntime, code: OpsCode, watch: WatchRow, reason: str) -> None:
    report_ops_event(
        runtime.session_factory,
        OpsEvent(code, watch.tenant_id, reason=reason, watch_id=watch.id, agent_id=watch.agent_id),
        logged=False,
    )


def _log_paused(watch_id: uuid.UUID, watch: WatchRow | None, reason: str) -> None:
    log.info(
        "watch.paused",
        tenant_id=str(watch.tenant_id) if watch is not None else None,
        watch_id=str(watch_id),
        reason=reason,
    )


async def run_poll(runtime: McpRuntime, watch_id: uuid.UUID, lease: str) -> bool:
    if not load_watches_settings().enabled:
        return False
    floor = 0
    watch: WatchRow | None = None
    try:
        async with asyncio.timeout(25):
            async with runtime.session_factory() as db:
                watch = await leased_poll(db, watch_id, lease, now=datetime.now(UTC))
            if watch is None:
                # Gate/expiry/lease races are scheduling outcomes, not owner revocation.
                # Step back in the claim queue: an Off watch claimed again every cycle
                # would fill all 4 claim slots and starve every other watch.
                until = datetime.now(UTC) + timedelta(seconds=OFF_DEFER_S)
                async with runtime.session_factory.begin() as db:
                    await defer_busy_poll(db, watch_id, lease, until=until)
                return False
            tool = str(watch.source_state["tool"])
            # The operator's current floor and credential, not those in place at approval.
            listed = require_listed(watch_server(watch.source, watch.source_state), tool)
            floor = listed.max_interval_floor_s
            arguments = TypeAdapter(dict[str, JsonValue]).validate_python(
                watch.source_state["arguments"]
            )
            expression, rule = str(watch.rule["projection"]), str(watch.rule["kind"])
            key = cast(str | None, watch.rule.get("key"))
            evidence_fields = cast(list[str], watch.rule.get("evidence") or [])
            interval = int(str(watch.source_state["interval"]))
            if watch.source == "mcp_poll":
                validate_mcp_poll(
                    arguments,
                    projection=expression,
                    rule=rule,
                    interval=interval,
                    key=key,
                    evidence=evidence_fields,
                )
            else:
                validate_poll(
                    tool,
                    arguments,
                    projection=expression,
                    rule=rule,
                    interval=interval,
                    key=key,
                    evidence=evidence_fields,
                )
            auth, read, server_name, agent_name = await authorize_source(runtime, watch)
            if not load_watches_settings().enabled:
                return False
            if server_name is None:
                document = await read_native(runtime, auth, read, watch)
                kind = "human_rows_v1"
            else:
                document = await read_mcp(
                    runtime,
                    watch,
                    server_name=server_name,
                    credential=listed.credential,
                    agent_name=agent_name,
                )
                kind = "mcp_v1"
            # The raw read is bounded and projected; only the projection is kept.
            projected = project(document, expression, limits=SOURCE, allow_map=True)
            value = kept(projected, rule=rule, key=key)
            previous = cast(JsonValue, watch.baseline.get("value"))
            # A legacy baseline may contain bot output or pagination metadata.
            # Re-establish it without firing; the version and value commit together.
            if isinstance(previous, dict) and previous.get("document") == kind:
                evidence = compare(
                    previous["projection"],
                    projected,
                    rule=cast(Rule, rule),
                    key=key,
                    evidence=evidence_fields,
                )
            else:
                # Validate added/updated baseline shape before saving the first value.
                compare(value, projected, rule=cast(Rule, rule), key=key)
                evidence = None
            saved: JsonValue = {"document": kind, "projection": value}
            bounded(saved, BASELINE)
            # Recheck membership/policy after network I/O before persisting evidence.
            await authorize_source(runtime, watch)
            async with runtime.session_factory.begin() as db:
                if await leased_poll(db, watch_id, lease, now=datetime.now(UTC)) is None:
                    await release_poll_lease(db, watch_id, lease)
                    return False
                await finish_poll(
                    db,
                    watch,
                    lease,
                    value=saved,
                    payload=None
                    if evidence is None
                    else evidence_payload(evidence, items=key is not None or bool(evidence_fields)),
                )
                await hold_to_floor(db, watch_id, floor=floor, now=datetime.now(UTC))
            return True
    except (SourceBusy, BotAccessUnavailable) as exc:
        _log_poll_failed(
            runtime, watch_id, watch, kind=type(exc).__name__, benign=isinstance(exc, SourceBusy)
        )
        # This server's own budget is spent, or the bot cannot read the source yet (not an
        # error, not a revocation). Step back in the claim queue, or due
        # watches on one busy server would fill every claim and starve the rest.
        until = datetime.now(UTC) + timedelta(seconds=BUSY_DEFER_S + random.uniform(0, 30))
        async with runtime.session_factory.begin() as db:
            await defer_busy_poll(db, watch_id, lease, until=until)
        return False
    except WatchFeatureUnreadable:
        _log_poll_failed(runtime, watch_id, watch, kind="WatchFeatureUnreadable")
        # The feature could not be read, which is not Off: keep the watch, retry next claim.
        async with runtime.session_factory.begin() as db:
            await release_poll_lease(db, watch_id, lease)
        return False
    except ToolUnlisted:
        _log_poll_failed(runtime, watch_id, watch, kind="ToolUnlisted")
        # The operator took the tool off the registry: pause and tell the owner.
        async with runtime.session_factory.begin() as db:
            paused = await pause_unlisted(db, watch_id, lease)
        if paused:
            _log_paused(watch_id, watch, "tool_unlisted")
        return False
    except SourceChanged:
        _log_poll_failed(runtime, watch_id, watch, kind="SourceChanged")
        # The server no longer offers what the owner approved: pause and tell them.
        async with runtime.session_factory.begin() as db:
            paused = await pause_unlisted(db, watch_id, lease, reason="source_changed")
        if paused:
            _log_paused(watch_id, watch, "source_changed")
        return False
    except CredentialMissing:
        _log_poll_failed(runtime, watch_id, watch, kind="CredentialMissing")
        # The owner forgot or never sent their key: pause and tell them how to send it.
        async with runtime.session_factory.begin() as db:
            paused = await pause_unlisted(db, watch_id, lease, reason="credential_missing")
        if paused:
            _log_paused(watch_id, watch, "credential_missing")
        return False
    except Exception as exc:
        # Never log remote content, arguments, credentials, lease capabilities or
        # exception text. A remote isError or transport failure (McpCallError) is
        # transient: it backs off and stops the watch only after a day of errors.
        from fastmcp.exceptions import ToolError

        capped = isinstance(exc, DBAPIError) and getattr(exc.orig, "sqlstate", None) == "54000"
        async with runtime.session_factory.begin() as db:
            failure = await poll_failed(
                db,
                watch_id,
                lease,
                capped=capped,
                daily_cap=daily_cap(exc) if capped and isinstance(exc, DBAPIError) else None,
                revoked=isinstance(
                    exc,
                    (
                        PollAccessRevoked,
                        PollBindingRevoked,
                        ToolError,
                        discord.Forbidden,
                        discord.NotFound,
                        NotFoundError,
                        PermissionDeniedError,
                        AccessPolicyUnreadable,
                    ),
                ),
                now=datetime.now(UTC),
            )
            if floor:
                await hold_to_floor(db, watch_id, floor=floor, now=datetime.now(UTC))
        _log_poll_failed(
            runtime,
            watch_id,
            watch,
            kind="daily_cap" if capped else "other",
            benign=capped,
            error_class=type(exc).__name__,
            consecutive_errors=failure.consecutive_errors if failure else None,
        )
        if failure is not None and failure.paused_reason == "failed":
            log.warning(
                "watch.paused_failing",
                tenant_id=str(failure.tenant_id),
                watch_id=str(watch_id),
                consecutive_errors=failure.consecutive_errors,
            )
            if watch is not None:
                _report_watch(runtime, "watch.paused_failing", watch, type(exc).__name__)
        elif failure is not None and failure.paused_reason is not None:
            _log_paused(watch_id, watch, failure.paused_reason)
        return False


def poll_endpoint(runtime: McpRuntime):
    active = 0
    recent: deque[float] = deque()

    async def endpoint(request: Request) -> Response:
        nonlocal active
        if not load_watches_settings().enabled:
            return Response(status_code=503)
        secret = load_watches_settings().executor_secret
        supplied = request.headers.getlist("X-Watch-Executor-Key")
        if (
            secret is None
            or len(supplied) != 1
            or not secrets.compare_digest(supplied[0].encode(), secret.get_secret_value().encode())
        ):
            return Response(status_code=403)
        # Authenticate and check a live opaque capability before charging shared budgets.
        try:
            watch_id = uuid.UUID(request.path_params["watch_id"])
            values = request.headers.getlist("X-Watch-Lease")
            if len(values) != 1:
                raise ValueError("Missing or duplicate lease")
            lease = values[0]
            uuid.UUID(lease)
        except ValueError:
            return Response(status_code=403)
        async with runtime.session_factory() as db:
            from daimon.core.stores.watches import get_watch

            watch = await get_watch(db, watch_id)
        if (
            watch is None
            or watch.lease_owner != lease
            or watch.lease_expires_at is None
            or watch.lease_expires_at <= datetime.now(UTC)
        ):
            return Response(status_code=403)
        now = time.monotonic()
        while recent and recent[0] <= now - 60:
            recent.popleft()
        if active >= 4 or len(recent) >= 120:
            return Response(status_code=429)
        recent.append(now)
        active += 1
        try:
            success = await run_poll(runtime, watch_id, lease)
            return Response(status_code=204 if success else 403)
        finally:
            active -= 1

    return endpoint
