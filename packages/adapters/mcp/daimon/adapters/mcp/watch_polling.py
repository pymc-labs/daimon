"""Trusted OS executor for sidecar leases, using existing owner-authorized reads.

The lease is an opaque short-lived scheduling capability, not a caller-selected
identity. No source data or tool arguments cross the sidecar transport. Model
clients are used only to retrieve current agent facts; no AI turn is started.
"""

from __future__ import annotations

import asyncio
import secrets
import time
import uuid
from collections import deque
from datetime import UTC, datetime
from typing import cast

import discord
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
from daimon.core.authz import Action, Place, Surface, authorize, build_agent_ref
from daimon.core.config import load_watches_settings
from daimon.core.continuity.poll_tools import (
    ToolUnlisted,
    require_distinct_source,
    require_listed,
    validate_poll,
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
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.membership_subject import resolve_subject
from daimon.core.mux_backend import resource_scope
from daimon.core.mux_compat import retrieve_agent
from daimon.core.stores.access_policy import AccessPolicyUnreadable, load_access_policy
from daimon.core.stores.accounts import get_account
from daimon.core.stores.domain import WatchRow
from daimon.core.stores.tenants import get_tenant
from daimon.core.stores.watches import (
    DailyCap,
    PollBindingRevoked,
    finish_poll,
    hold_to_floor,
    leased_poll,
    pause_unlisted,
    poll_failed,
    release_poll_lease,
)
from daimon.core.tool_safety import DAIMON_SERVER_NAME, ToolCall
from daimon.core.turn.approvals import agent_rule_lookup
from pydantic import JsonValue, TypeAdapter
from sqlalchemy.exc import DBAPIError
from starlette.requests import Request
from starlette.responses import Response


class PollAccessRevoked(ValueError):
    pass


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
        channel = _require_guild_channel(
            await _resolve_channel(client, resource_id), auth.external_id
        )
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
    if watch.source == "poll":
        # The operator registry, read fresh each time: before and after every read.
        require_listed(DAIMON_SERVER_NAME, str(watch.source_state.get("tool")))
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
        runtime.client, watch.agent_id, scope=resource_scope(tenant_id=str(watch.tenant_id))
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
            raise PollAccessRevoked("OS endpoint unavailable")
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
            raise PollAccessRevoked("Agent OS toolset unavailable")
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
        source_id = str(arguments.get("thread_id", arguments.get("channel_id", "")))
        try:
            require_distinct_source(arguments, watch.thread_id)
        except ValueError as exc:
            raise PollAccessRevoked(str(exc)) from exc
        source_place = await poll_read_place(runtime, auth, source_id)
        # Identity equality follows native resolution, never caller spelling.
        if source_place.channel_id == place.channel_id:
            raise PollAccessRevoked("Poll source resolves to the reporting thread")
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
    return auth, read


async def run_poll(runtime: McpRuntime, watch_id: uuid.UUID, lease: str) -> bool:
    if not load_watches_settings().enabled:
        return False
    floor = 0
    try:
        async with asyncio.timeout(25):
            async with runtime.session_factory() as db:
                watch = await leased_poll(db, watch_id, lease, now=datetime.now(UTC))
            if watch is None:
                # Gate/expiry/lease races are scheduling outcomes, not owner revocation.
                async with runtime.session_factory.begin() as db:
                    await release_poll_lease(db, watch_id, lease)
                return False
            tool = str(watch.source_state["tool"])
            # The operator's current floor, not the one in place at approval.
            floor = require_listed(DAIMON_SERVER_NAME, tool).max_interval_floor_s
            arguments = TypeAdapter(dict[str, JsonValue]).validate_python(
                watch.source_state["arguments"]
            )
            expression, rule = str(watch.rule["projection"]), str(watch.rule["kind"])
            key = cast(str | None, watch.rule.get("key"))
            evidence_fields = cast(list[str], watch.rule.get("evidence") or [])
            validate_poll(
                tool,
                arguments,
                projection=expression,
                rule=rule,
                interval=int(str(watch.source_state["interval"])),
                key=key,
                evidence=evidence_fields,
            )
            auth, read = await authorize_poll(runtime, watch)
            if not load_watches_settings().enabled:
                return False
            limit = int(str(arguments.get("limit", 10)))
            if tool == "read_thread":
                document = await _read_thread_impl(
                    runtime,
                    auth,
                    thread_id=str(arguments["thread_id"]),
                    limit=limit,
                    read_policy=read,
                    require_owner_history=True,
                    poll_humans_only=True,
                )
            else:
                document = await _read_channel_impl(
                    runtime,
                    auth,
                    channel_id=str(arguments["channel_id"]),
                    limit=limit,
                    read_policy=read,
                    require_owner_history=True,
                    poll_humans_only=True,
                )
            # Keep only human-authored rows; no pagination metadata or assistant
            # output can enter any projection, including identity (".").
            raw = document.model_dump(mode="json")
            human_document: JsonValue = {
                "rows": [row for row in raw["rows"] if row.get("role") == "user"]
            }
            # The raw read is bounded (I7) and projected; only the projection is kept.
            projected = project(human_document, expression, limits=SOURCE, allow_map=True)
            value = kept(projected, rule=rule, key=key)
            previous = cast(JsonValue, watch.baseline.get("value"))
            # A legacy baseline may contain bot output or pagination metadata.
            # Re-establish it without firing; the version and value commit together.
            if isinstance(previous, dict) and previous.get("document") == "human_rows_v1":
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
            saved: JsonValue = {"document": "human_rows_v1", "projection": value}
            bounded(saved, BASELINE)
            # Recheck membership/policy after network I/O before persisting evidence.
            await authorize_poll(runtime, watch)
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
    except ToolUnlisted:
        # The operator took the tool off the registry: pause and tell the owner.
        async with runtime.session_factory.begin() as db:
            await pause_unlisted(db, watch_id, lease)
        return False
    except Exception as exc:
        # Never log remote content, arguments, lease capabilities or exception text.
        from fastmcp.exceptions import ToolError

        capped = isinstance(exc, DBAPIError) and getattr(exc.orig, "sqlstate", None) == "54000"
        async with runtime.session_factory.begin() as db:
            await poll_failed(
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
