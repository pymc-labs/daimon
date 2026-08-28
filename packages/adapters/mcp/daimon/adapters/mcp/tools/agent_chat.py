"""Agent-chat primitives plus bounded ``ask`` and completed-chart delivery.

These tools are tagged ``"agent-chat"`` and hidden by default via the
``Visibility(False, tags={"agent-chat"})`` baseline in ``server.py``. They
surface only when the middleware narrowing fires (i.e. the token carries a
valid derived-UUID ``agent_id`` claim).

Agent identity is read SERVER-SIDE from ``auth.agent_id`` (the verified
derived UUID). No tool accepts ``agent_id`` as a parameter — this prevents
confused-deputy attacks where a caller claims to be a different agent. Every
session-handle tool re-derives the session agent's UUID and rejects handles
that aren't this agent's (cross-tenant AND same-tenant cross-agent, WR-03).

Headless loop (primitives plus the bounded ``ask`` convenience tool):
- ``start_turn`` creates a persistent MA session (via
  ``daimon.core.sessions.create_session`` for vault/repo/env parity) and sends
  the first message; returns ``{"handle": <session_id>}``. Admission-gated:
  runs the same balance/cap checks as the billed media tools before creating
  a session.
- ``continue_turn`` sends a follow-up ``user.message``. Admission-gated,
  same as ``start_turn``.
- ``get_session`` returns status/metadata (poll until idle). Read-only, not
  gated.
- ``list_events`` returns the transcript; the caller reads the reply from the
  ``agent.message`` events. Agents run ``permission_policy=always_allow``
  (``specs.py``), so sessions reach idle without tool confirmations.
  Read-only, not gated.
- ``list_sessions`` enumerates this agent's sessions for resume. Read-only,
  not gated.
- ``ask`` starts a turn, polls until idle for at most two minutes, and returns
  the final text plus bounded chart images and optional presigned links.
  Admission-gated, same as ``start_turn``.
- ``deliver_turn_charts`` finds the newest completed reply and delivers charts
  from that turn. It performs a bounded artifact-store write when optional
  link delivery is configured; it is not admission-gated.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from time import monotonic
from typing import Any, Literal

from anthropic.types.beta import (
    BetaEnvironment,
    BetaManagedAgentsAgent,
    BetaManagedAgentsSession,
)
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.hosted_artifacts import ChartUrl, deliver_hosted_charts
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools._ctx import (
    _auth,  # pyright: ignore[reportPrivateUsage]
    _check_admission,  # pyright: ignore[reportPrivateUsage]
)
from daimon.adapters.mcp.tools._pagination import Page
from daimon.adapters.mcp.tools.sessions import SessionEventOut, SessionInfo
from daimon.core.billing import BillingConfig
from daimon.core.defaults.ma_index import find_environment_by_daimon_tag, list_agents_by_tenant
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.scope import ScopeContext
from daimon.core.sessions import create_session
from daimon.core.stores.agent_repo_binding import get_binding
from daimon.core.stores.scoped_config_read import resolve
from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.tools import ToolResult
from pydantic import BaseModel, Field
from pydantic.json_schema import SkipJsonSchema

from mcp.types import ImageContent, TextContent

# Each transcript page costs two upstream calls (ownership retrieve + events
# list), so an uncapped walk on a long session can outlast any client timeout.
_MAX_EVENT_PAGES = 20


class AgentDescription(BaseModel):
    """Read-only description of a Daimon agent, surfaced for coding agents."""

    model_config = {"frozen": True}

    name: str
    role_summary: str
    skill_names: list[str]
    repo_url: str | None
    environment_name: str | None


class AskResult(BaseModel):
    """Final text, chart capabilities, and resumable handle returned by ``ask``."""

    model_config = {"frozen": True}

    handle: str
    message: str
    chart_urls: tuple[ChartUrl, ...] = ()
    # Excluded from dumps AND the JSON schema: image blocks travel as MCP
    # content, never inside structured content, and advertising the field
    # would point schema-reading clients at a value that never arrives.
    image_blocks: SkipJsonSchema[tuple[ImageContent, ...]] = Field(
        default=(), exclude=True, repr=False
    )


def _ask_tool_result(result: AskResult) -> ToolResult:
    """Give the model prose plus image blocks alongside the typed payload.

    Built the same way with or without charts, so the caller sees one content
    shape: prose text first, image blocks after, structured payload throughout.
    """
    return ToolResult(
        content=[TextContent(type="text", text=result.message), *result.image_blocks],
        structured_content=result.model_dump(mode="json"),
    )


class _DeliveryCache:
    """Absorb hosted-client retry loops on ``deliver_turn_charts``.

    A repeated call for the same completed turn re-downloads every chart from
    the Files API and re-writes the artifact store on the operator's account,
    unmetered — so one delivered result is held briefly per turn boundary.
    """

    def __init__(
        self,
        *,
        max_entries: int = 128,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        self._entries: OrderedDict[tuple[str, str], tuple[float, AskResult]] = OrderedDict()
        self._max_entries = max_entries
        self._clock = clock

    def get(self, key: tuple[str, str]) -> AskResult | None:
        entry = self._entries.get(key)
        if entry is None:
            return None
        expires_at, result = entry
        if self._clock() >= expires_at:
            del self._entries[key]
            return None
        return result

    def put(self, key: tuple[str, str], result: AskResult, *, ttl_seconds: float) -> None:
        if ttl_seconds <= 0:
            return
        self._entries[key] = (self._clock() + ttl_seconds, result)
        self._entries.move_to_end(key)
        while len(self._entries) > self._max_entries:
            self._entries.popitem(last=False)


_DELIVERY_CACHE_TTL_SECONDS = 300.0
_delivery_cache = _DeliveryCache()


async def _resolve_environment_name(
    runtime: McpRuntime,
    auth: AuthIdentity,
) -> str | None:
    """Resolve environment_name through the shared channel/tenant/deployment cascade.

    MCP has no channel, so ``ScopeContext.channel_id`` stays None and the
    cascade falls through tenant -> ``runtime.deployment_default``. This is
    the same shared ``resolve()`` the Discord adapter uses (parity fix,
    MPP-01) — no second tenant-row-only resolution path.
    """
    async with runtime.session_factory() as session:
        resolved = await resolve(
            session,
            context=ScopeContext(tenant_id=auth.tenant_id, account_id=auth.account_id),
            default=runtime.deployment_default,
        )
    return resolved.environment_name


async def _verify_agent_owns_session(
    runtime: McpRuntime,
    auth: AuthIdentity,
    handle: str,
) -> BetaManagedAgentsSession:
    """Assert the caller's agent owns the session, not merely its tenant.

    Stricter than ``_verify_tenant_owns_session``: re-derives the session
    agent's UUID and compares it to ``auth.agent_id`` (the verified derived
    per-agent UUID). Rejects sibling-agent handles within the same tenant
    (WR-03). Raises ``ToolError("session not found")`` — same message as a
    genuinely missing session, so existence isn't leaked across agents.
    """
    s = await runtime.client.beta.sessions.retrieve(handle)
    derived = derive_agent_uuid(tenant_id=auth.tenant_id, ma_agent_id=str(s.agent.id))
    if auth.agent_id is None or derived != auth.agent_id:
        raise ToolError("session not found")
    return s


async def _resolve_ma_agent(
    runtime: McpRuntime,
    auth: AuthIdentity,
) -> BetaManagedAgentsAgent:
    """Resolve the MA agent for this caller from the verified claim.

    Matches ``auth.agent_id`` (the derived per-agent UUID) against all
    tenant agents by re-deriving the UUID for each. Raises ToolError if
    no match — fails closed (never falls back to a broader scope).
    """
    if auth.agent_id is None:
        raise ToolError("agent not found")
    agents = await list_agents_by_tenant(runtime.client, tenant_id=auth.tenant_id)
    for agent in agents:
        derived = derive_agent_uuid(tenant_id=auth.tenant_id, ma_agent_id=str(agent.id))
        if derived == auth.agent_id:
            return agent
    raise ToolError("agent not found")


async def _describe_agent_impl(
    runtime: McpRuntime,
    auth: AuthIdentity,
) -> AgentDescription:
    """Resolve the agent from the claim and return a structured description."""
    ma_agent = await _resolve_ma_agent(runtime, auth)
    env_name = await _resolve_environment_name(runtime, auth)
    if auth.agent_id is None:
        raise ToolError("agent not found")
    async with runtime.session_factory() as session:
        binding = await get_binding(session, tenant_id=auth.tenant_id, agent_id=auth.agent_id)
    repo_url = binding.repo_url if binding is not None else None
    skill_names = [sk.skill_id for sk in ma_agent.skills]
    role_summary = (ma_agent.system or "")[:200]
    return AgentDescription(
        name=ma_agent.name,
        role_summary=role_summary,
        skill_names=skill_names,
        repo_url=repo_url,
        environment_name=env_name,
    )


async def _start_turn_impl(
    runtime: McpRuntime,
    auth: AuthIdentity,
    message: str,
) -> dict[str, str]:
    """Create a new MA session for the caller's agent and send the first message.

    Returns ``{"handle": session_id}`` — the caller passes ``handle`` to
    subsequent ``continue_turn``, ``get_session``, and ``list_events`` calls.

    Environment resolution (fail-closed):
    - Resolve ``environment_name`` via the shared channel/tenant/deployment
      cascade (``resolve()``, MPP-01 — same as Discord).
    - Look it up on MA via ``find_environment_by_daimon_tag``.
    - Raise ``ToolError("environment not found")`` if either step fails.
    """
    ma_agent = await _resolve_ma_agent(runtime, auth)

    env_name = await _resolve_environment_name(runtime, auth)
    if env_name is None:
        raise ToolError("environment not found")

    environment: BetaEnvironment | None = await find_environment_by_daimon_tag(
        runtime.client, tenant_id=auth.tenant_id, name=env_name
    )
    if environment is None:
        raise ToolError("environment not found")

    github_fallback_pat: str | None = (
        runtime.settings.github.fallback_pat.get_secret_value()
        if runtime.settings.github.fallback_pat is not None
        else None
    )
    github_app_id: str | None = runtime.settings.github.app_id
    github_app_private_key: str | None = (
        runtime.settings.github.app_private_key.get_secret_value()
        if runtime.settings.github.app_private_key is not None
        else None
    )

    session = await create_session(
        runtime.client,
        agent=ma_agent,
        environment=environment,
        mcp_settings=runtime.settings.mcp,
        account_id=auth.account_id,
        tenant_id=auth.tenant_id,
        agent_uuid=auth.agent_id,
        session_factory=runtime.session_factory,
        fernet=runtime.fernet,
        github_fallback_pat=github_fallback_pat,
        github_app_id=github_app_id,
        github_app_private_key=github_app_private_key,
    )

    await runtime.client.beta.sessions.events.send(
        session.id,
        events=[
            {
                "type": "user.message",
                "content": [{"type": "text", "text": message}],
            }
        ],
    )

    return {"handle": session.id}


async def _continue_turn_impl(
    runtime: McpRuntime,
    auth: AuthIdentity,
    handle: str,
    message: str,
) -> dict[str, str]:
    """Send a follow-up message on an existing session.

    ``_verify_agent_owns_session`` guards against cross-tenant AND
    same-tenant cross-agent handles (Tampering threat mitigation, WR-03).
    """
    await _verify_agent_owns_session(runtime, auth, handle)
    await runtime.client.beta.sessions.events.send(
        handle,
        events=[
            {
                "type": "user.message",
                "content": [{"type": "text", "text": message}],
            }
        ],
    )
    return {"handle": handle}


async def _list_sessions_impl(
    runtime: McpRuntime,
    auth: AuthIdentity,
) -> list[SessionInfo]:
    """List sessions for THIS caller's agent only (agent-scoped, not tenant).

    Resolves the caller's MA agent from the verified claim, then lists only
    that agent's sessions. Other agents' sessions in the same tenant are never
    returned — the headless token is scoped to its own agent.
    """
    ma_agent = await _resolve_ma_agent(runtime, auth)
    results: list[SessionInfo] = []
    async for s in runtime.client.beta.sessions.list(agent_id=str(ma_agent.id)):
        results.append(SessionInfo.from_ma(s))
    return results


async def _get_session_impl(
    runtime: McpRuntime,
    auth: AuthIdentity,
    handle: str,
) -> SessionInfo:
    """Return one session's status/metadata (no reply text, no side effects).

    ``_verify_agent_owns_session`` guards cross-tenant AND same-tenant
    cross-agent handles (WR-03).
    """
    s = await _verify_agent_owns_session(runtime, auth, handle)
    return SessionInfo.from_ma(s)


async def _list_events_impl(
    runtime: McpRuntime,
    auth: AuthIdentity,
    handle: str,
    page: str | None,
    limit: int | None,
    order: Literal["asc", "desc"] | None,
) -> Page[SessionEventOut]:
    """List a session's events (the transcript) for the caller to read.

    This is how a primitives-only caller reads the agent's reply: fold the
    ``agent.message`` events client-side. ``_verify_agent_owns_session``
    guards cross-tenant AND same-tenant cross-agent handles (WR-03).
    """
    await _verify_agent_owns_session(runtime, auth, handle)
    list_kwargs: dict[str, Any] = {}
    if page is not None:
        list_kwargs["page"] = page
    if limit is not None:
        list_kwargs["limit"] = limit
    if order is not None:
        list_kwargs["order"] = order
    cursor = await runtime.client.beta.sessions.events.list(handle, **list_kwargs)
    return Page[SessionEventOut](
        items=[
            SessionEventOut.model_validate(event.model_dump(mode="json")) for event in cursor.data
        ],
        next_page=cursor.next_page,
    )


def _agent_message_text(event: SessionEventOut) -> str | None:
    """Fold the text blocks from one ``agent.message`` event."""
    if event.type != "agent.message":
        return None
    text = "\n".join(
        str(block["text"])
        for block in (event.content or [])
        if block.get("type") == "text" and block.get("text") is not None
    )
    return text or None


def _event_timestamp(event: SessionEventOut) -> dt.datetime | None:
    """Read an event timestamp across current Managed Agents projections."""
    value = getattr(event, "processed_at", None)
    if value is None:
        value = getattr(event, "created_at", None)
    if isinstance(value, dt.datetime):
        return value if value.tzinfo else value.replace(tzinfo=dt.UTC)
    if isinstance(value, str):
        try:
            parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.UTC)
    return None


async def _deliver_turn_charts_impl(
    runtime: McpRuntime,
    auth: AuthIdentity,
    handle: str,
) -> AskResult:
    """Deliver the newest completed turn's text and charts without starting a turn."""
    session = await _get_session_impl(runtime, auth, handle)
    if session.status not in {"idle", "terminated"}:
        raise ToolError(
            f"session {handle} is {session.status!r}; deliver charts only after "
            "get_my_session reports 'idle' or 'terminated'"
        )

    page: str | None = None
    final_text: str | None = None
    turn_started_at: dt.datetime | None = None
    for _ in range(_MAX_EVENT_PAGES):
        events = await _list_events_impl(runtime, auth, handle, page, 100, "desc")
        for event in events.items:
            if final_text is None:
                final_text = _agent_message_text(event)
                continue
            if event.type == "user.message":
                turn_started_at = _event_timestamp(event) or session.created_at
                break
        if turn_started_at is not None or events.next_page is None:
            break
        page = events.next_page

    if final_text is None:
        raise ToolError(f"no completed turn found for session {handle}")
    if turn_started_at is None:
        raise ToolError(f"no completed turn boundary found for session {handle}")

    cache_key = (handle, turn_started_at.isoformat())
    cached = _delivery_cache.get(cache_key)
    if cached is not None:
        return cached

    delivery = await deliver_hosted_charts(
        runtime.client,
        settings=runtime.settings.artifacts,
        tenant_id=str(auth.tenant_id),
        account_id=str(auth.account_id),
        session_id=handle,
        turn_started_at=turn_started_at,
        message=final_text,
        store=runtime.artifact_store,
    )
    result = AskResult(
        handle=handle,
        message=delivery.message,
        chart_urls=delivery.chart_urls,
        image_blocks=delivery.image_blocks,
    )
    ttl = _DELIVERY_CACHE_TTL_SECONDS
    if result.chart_urls:
        # A cached entry must never outlive its presigned links.
        earliest = min(chart.expires_at for chart in result.chart_urls)
        ttl = min(ttl, (earliest - dt.datetime.now(dt.UTC)).total_seconds())
    _delivery_cache.put(cache_key, result, ttl_seconds=ttl)
    return result


async def _ask_impl(
    runtime: McpRuntime,
    auth: AuthIdentity,
    message: str,
    *,
    timeout_seconds: float = 120.0,
    poll_interval_seconds: float = 1.0,
    clock: Callable[[], float] = monotonic,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    now: Callable[[], dt.datetime] = lambda: dt.datetime.now(dt.UTC),
) -> AskResult:
    """Start a turn, wait boundedly for idle, and return its final reply."""
    turn_started_at = now()
    started = await _start_turn_impl(runtime, auth, message)
    handle = started["handle"]
    deadline = clock() + timeout_seconds

    while True:
        current = await _get_session_impl(runtime, auth, handle)
        # Only terminated is treated as terminal: the status value set is
        # upstream-controlled (see SessionInfo.status), so an unmodeled
        # transient status keeps polling until the deadline instead of
        # failing a turn that admission already billed.
        if current.status == "terminated":
            raise ToolError(
                f"Daimon turn reached terminal status {current.status!r}; "
                f"resume or inspect events with handle {handle}"
            )
        if current.status == "idle":
            page: str | None = None
            final_text: str | None = None
            for _ in range(_MAX_EVENT_PAGES):
                events = await _list_events_impl(runtime, auth, handle, page, 100, "desc")
                for event in events.items:
                    final_text = _agent_message_text(event)
                    if final_text is not None:
                        break
                if final_text is not None or events.next_page is None:
                    break
                page = events.next_page

            if final_text is not None:
                delivery = await deliver_hosted_charts(
                    runtime.client,
                    settings=runtime.settings.artifacts,
                    tenant_id=str(auth.tenant_id),
                    account_id=str(auth.account_id),
                    session_id=handle,
                    turn_started_at=turn_started_at,
                    message=final_text,
                    store=runtime.artifact_store,
                )
                return AskResult(
                    handle=handle,
                    message=delivery.message,
                    chart_urls=delivery.chart_urls,
                    image_blocks=delivery.image_blocks,
                )

        remaining = deadline - clock()
        if remaining <= 0:
            raise ToolError(
                f"Daimon turn did not become idle with a reply within "
                f"{timeout_seconds:g} seconds; resume with handle {handle}"
            )
        await sleep(min(poll_interval_seconds, remaining))


def register_agent_chat_tools(
    mcp: FastMCP,
    runtime: McpRuntime,
    *,
    billing_config: BillingConfig | None,
) -> None:
    """Register the agent-chat tools on ``mcp``.

    All tools carry ``tags={"agent-chat"}`` so the
    ``Visibility(False, tags={"agent-chat"})`` baseline in ``server.py``
    hides them by default, and the middleware narrowing reveals them when the
    token carries a valid ``agent_id`` claim.

    The underlying surface mirrors the CMA session/events API, scoped to the
    caller's agent. ``ask`` composes the same primitives into one bounded
    hosted-client call and adds chart delivery after the session becomes idle.
    ``deliver_turn_charts`` provides the same delivery for clients that poll
    and read the primitives themselves.

    ``list_sessions``/``get_session`` are registered as ``list_my_sessions``/
    ``get_my_session`` to avoid a name collision with the tenant-scoped
    operator tools of the same name (the headless caller only ever sees this
    agent-chat set, so the "my" prefix is harmless and reads as agent-scoped).

    ``start_turn``, ``continue_turn``, and ``ask`` run the shared
    ``_check_admission`` gate (the same balance/cap checks the media tools run)
    before creating a session or sending an event. The four read-only tools and
    ``deliver_turn_charts`` stay on bare ``_auth``; the latter performs a
    bounded artifact-store write when optional link delivery is configured.
    """

    @mcp.tool(tags={"agent-chat"})  # pyright: ignore[reportArgumentType]
    async def describe_agent(ctx: Context) -> AgentDescription:  # pyright: ignore[reportUnusedFunction]
        """Describe the agent associated with this MCP token.

        Returns the agent's name, system-prompt summary, skill identifiers,
        and configured environment. No parameters — agent identity is read
        from the token claim (confused-deputy mitigation).
        """
        return await _describe_agent_impl(runtime, await _auth(ctx))

    @mcp.tool(tags={"agent-chat"}, name="list_my_sessions")  # pyright: ignore[reportArgumentType]
    async def list_my_sessions(ctx: Context) -> list[SessionInfo]:  # pyright: ignore[reportUnusedFunction]
        """List this agent's sessions (id, status, title, timestamps).

        ``id`` is the handle you pass to ``get_session``, ``list_events``,
        ``deliver_turn_charts``, and ``continue_turn``. Scoped to this agent
        only. No parameters — identity is read from the token claim.
        """
        return await _list_sessions_impl(runtime, await _auth(ctx))

    @mcp.tool(tags={"agent-chat"})  # pyright: ignore[reportArgumentType]
    async def start_turn(ctx: Context, message: str) -> dict[str, str]:  # pyright: ignore[reportUnusedFunction]
        """Start a new conversation turn with the agent and return a handle.

        Creates a persistent MA session with vault/repo/env parity and sends
        the first user message. Returns ``{"handle": "<session_id>"}`` which
        you pass to ``get_session``, ``list_events``, and ``continue_turn``.
        """
        auth = await _check_admission(
            ctx,
            sessionmaker=runtime.session_factory,
            billing_config=billing_config,
            tool_name="start_turn",
        )
        return await _start_turn_impl(runtime, auth, message)

    @mcp.tool(tags={"agent-chat"})  # pyright: ignore[reportArgumentType]
    async def ask(ctx: Context, message: str) -> AskResult:  # pyright: ignore[reportUnusedFunction]
        """Ask one question and wait for the final answer and any chart images. Starts a
        persistent turn, waits up to about 120 seconds for idle, and
        returns the final text plus a resumable handle. Chart images are
        embedded by default; short-lived download links are added when private
        artifact storage is configured.
        """
        auth = await _check_admission(
            ctx,
            sessionmaker=runtime.session_factory,
            billing_config=billing_config,
            tool_name="ask",
        )
        return _ask_tool_result(  # type: ignore[return-value]
            await _ask_impl(runtime, auth, message)
        )

    @mcp.tool(tags={"agent-chat"})  # pyright: ignore[reportArgumentType]
    async def continue_turn(ctx: Context, handle: str, message: str) -> dict[str, str]:  # pyright: ignore[reportUnusedFunction]
        """Send a follow-up message on an existing session.

        Use this to continue a multi-turn conversation. Returns
        ``{"handle": "<session_id>"}`` unchanged for chaining.
        """
        auth = await _check_admission(
            ctx,
            sessionmaker=runtime.session_factory,
            billing_config=billing_config,
            tool_name="continue_turn",
        )
        return await _continue_turn_impl(runtime, auth, handle, message)

    @mcp.tool(tags={"agent-chat"}, name="get_my_session")  # pyright: ignore[reportArgumentType]
    async def get_my_session(ctx: Context, handle: str) -> SessionInfo:  # pyright: ignore[reportUnusedFunction]
        """Get one session's status and metadata (no reply text, read-only).

        Use this to check whether a turn has finished (``status`` becomes
        ``idle``/``terminated``) before reading the transcript with
        ``list_events``.
        """
        return await _get_session_impl(runtime, await _auth(ctx), handle)

    @mcp.tool(tags={"agent-chat"})  # pyright: ignore[reportArgumentType]
    async def list_events(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        handle: str,
        page: str | None = None,
        limit: int | None = None,
        order: Literal["asc", "desc"] | None = None,
    ) -> Page[SessionEventOut]:
        """List a session's events — the transcript.

        The agent's reply is in the ``agent.message`` events. Call this once
        ``get_session`` reports the session is idle to read what the agent said.
        """
        return await _list_events_impl(runtime, await _auth(ctx), handle, page, limit, order)

    @mcp.tool(tags={"agent-chat"})  # pyright: ignore[reportArgumentType]
    async def deliver_turn_charts(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        handle: str,
    ) -> AskResult:
        """Return the newest completed reply with its chart images and optional links. Use
        after ``get_my_session`` reports ``idle`` or ``terminated``; calls made
        while a turn is running or rescheduling are refused. This tool does not
        start a turn or send an event, but optional link delivery writes the
        chart to the configured private artifact store. The handle must belong
        to the agent in this token.
        """
        return _ask_tool_result(  # type: ignore[return-value]
            await _deliver_turn_charts_impl(runtime, await _auth(ctx), handle)
        )
