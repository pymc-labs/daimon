"""Agent-chat primitives plus bounded ``ask`` and completed-chart delivery.

These tools are tagged ``"agent-chat"`` and hidden by default via the
``Visibility(False, tags={"agent-chat"})`` baseline in ``server.py``. They
surface only when the middleware narrowing fires (i.e. the token carries a
valid derived-UUID ``agent_id`` claim).

Agent identity is read SERVER-SIDE from ``auth.agent_id`` (the verified
derived UUID). No tool accepts ``agent_id`` as a parameter — this prevents
confused-deputy attacks where a caller claims to be a different agent. Every
session-handle tool re-derives the session agent's UUID and rejects handles
that aren't this agent's (cross-tenant AND same-tenant cross-agent).

Headless loop (primitives plus the bounded ``ask`` convenience tool):
- ``start_turn`` creates a persistent MA session (via
  ``daimon.core.sessions.create_session`` for vault/repo/env parity) and sends
  the first message; returns ``{"handle": <session_id>}``. Admission-gated:
  runs the same balance/cap checks as the billed media tools before creating
  a session.
- ``continue_turn`` sends a follow-up ``user.message``. Admission-gated,
  same as ``start_turn``. On a paused session it first resumes the paused
  turn (see below); the message is then optional.
- ``await_turn`` waits on the session's event stream and returns when the turn
  has news: the agent said something, the turn ended, or it stopped. A paused
  turn is resumed first, as ``continue_turn`` does, so a long turn runs to its
  end while its caller waits. Not gated; the resume is.
- ``get_session`` returns status/metadata. Read-only, not gated.
- ``list_events`` returns the transcript; the caller reads the reply from the
  ``agent.message`` events. Read-only, not gated.

Nobody watches these turns, so they run under the unattended tool policy;
``_agent_chat_safety`` says how their pauses are answered.
- ``list_sessions`` enumerates this agent's sessions for resume. Read-only,
  not gated.
- ``ask`` starts a turn, waits on the event stream for at most two minutes, and
  returns the final text plus bounded chart images and optional presigned links.
  Admission-gated, same as ``start_turn``.
- ``deliver_turn_charts`` finds the newest completed reply and delivers charts
  from that turn. It performs a bounded artifact-store write when optional
  link delivery is configured; it is not admission-gated.
- ``archive_my_session`` archives a finished session so it stops being a live
  session forever (a reader thread that never archives would otherwise be
  re-read by every usage-sweep tick). Read-only ownership check, not gated.
- ``cancel_turn`` sends one unconditional ``user.interrupt`` and reports the
  status observed right after — no read-then-send race. Read-only ownership
  check, not gated (a caller over balance must still be able to stop a turn).
- ``get_turn_cost`` folds one finished turn's ``span.model_request_end``
  events into a pre-markup USD cost. A separate tool, not a field on
  ``get_session`` — the host polls status every two seconds and reads cost
  once per terminal. Read-only ownership check, not gated.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Collection
from dataclasses import dataclass
from decimal import Decimal
from functools import partial
from time import monotonic
from typing import Any, Literal, cast

import anthropic
import structlog
from anthropic.types.beta import (
    BetaEnvironment,
    BetaManagedAgentsAgent,
    BetaManagedAgentsSession,
)
from anthropic.types.beta.session_create_params import Resource
from anthropic.types.beta.sessions import BetaManagedAgentsSendSessionEvents
from daimon.adapters.mcp.auth.resolver import AuthIdentity, token_channel_id
from daimon.adapters.mcp.hosted_artifacts import ChartUrl, deliver_hosted_charts
from daimon.adapters.mcp.resource_ports import (
    list_session_events as list_events_page,
)
from daimon.adapters.mcp.resource_ports import (
    mcp_scope,
    retrieve_file_metadata,
    retrieve_session,
    send_session_events,
    walk_sessions,
)
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools._agent_chat_safety import (
    PAUSED_HINT,
    RECHECK_S,
    TURN_STOPPED,
    TurnFeed,
    Woke,
    caught_up,
    earlier_turn_open,
    gate,
    local_worker,
    record_turn,
    recorded_turn_id,
    require_session_driver,
    resume_paused,
    rules_channel,
    single_attempt,
    turn_status,
    watch_turn,
    why_stopped,
)
from daimon.adapters.mcp.tools._channel_policy import load_channel_policy
from daimon.adapters.mcp.tools._ctx import (
    _admission_recheck,  # pyright: ignore[reportPrivateUsage]
    _auth,  # pyright: ignore[reportPrivateUsage]
    _check_admission,  # pyright: ignore[reportPrivateUsage]
    _gate_origin_spend,  # pyright: ignore[reportPrivateUsage]
    _policy_gate,  # pyright: ignore[reportPrivateUsage]
)
from daimon.adapters.mcp.tools._pagination import Page
from daimon.adapters.mcp.tools._session_access import (
    admin_readable_legacy_sessions,
    require_session_outside_seals,
    session_belongs_to_caller,
    sessions_outside_seals,
)
from daimon.adapters.mcp.tools._turn_observation import observed_agent_turn
from daimon.adapters.mcp.tools.sessions import SessionEventOut, SessionInfo
from daimon.core import bundle_handle
from daimon.core.agent_chat_requester import UNREAD, record_driver, session_provenance
from daimon.core.agent_mcp_credentials import sync_agent_mcp_credentials
from daimon.core.agent_pins import agent_pin_names as core_agent_pin_names
from daimon.core.billing import BillingConfig
from daimon.core.config import get_repo_memory_settings
from daimon.core.data_reading import AFTER_DATA_REFUSAL, AGENT_REQUEST_REFUSAL, reads_data
from daimon.core.defaults.ma_index import find_environment_by_daimon_tag
from daimon.core.defaults.metadata import (
    MA_METADATA_KEY_BUDGET_CHANNEL,
    MA_METADATA_KEY_ISOLATED,
    MA_METADATA_KEY_NAME,
    MA_METADATA_KEY_SOURCE_RELEASE,
    MA_METADATA_KEY_SOURCE_RELEASE_AT,
    MA_METADATA_KEY_SOURCE_RELEASE_URL,
    MA_METADATA_KEY_SPEC_HASH,
)
from daimon.core.features.snapshot import FeaturePlace
from daimon.core.features.toolset import place_session
from daimon.core.github_app_session import close_headless_app_session
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.ma_resolver import find_agent_by_derived_uuid_cached
from daimon.core.mcp_identity import identity_relay_urls
from daimon.core.mcp_vault import (
    archive_relay_vaults,
    refresh_mounted_mcp_vaults,
    refresh_relay_vault,
)
from daimon.core.monitoring import KeptQuestion, capture_question, monitored_notice
from daimon.core.mux_compat import retrieve_agent, retrieve_agent_version
from daimon.core.permissions import agent_permissions, channel_permissions, memory_writable
from daimon.core.ports.gate import data_sources_on
from daimon.core.pricing import MODEL_PRICING, cost_of
from daimon.core.scope import ScopeContext
from daimon.core.session_mutation import session_mutation_fence
from daimon.core.session_ports_compat import archive_session_record
from daimon.core.session_seal import child_seal_ids, seal_ids
from daimon.core.session_snapshot import (
    canonical_json,
)
from daimon.core.sessions import create_isolated_session, create_session
from daimon.core.stores import session_settle_state
from daimon.core.stores.data_memory import has_read
from daimon.core.stores.github_access import list_live_grant_repositories
from daimon.core.stores.github_issued_tokens import touch_unmapped_app_session
from daimon.core.stores.scoped_config_read import resolve
from daimon.core.turn.outcomes import current_outcome
from daimon.core.turn.posture import ExemptReason
from daimon.core.turn.reducers import refusal_error
from daimon.core.turn.termination import TerminationReason
from daimon.core.turn.unattended_capabilities import capability_policy
from daimon.core.turn.web_search_sources import claude_searches, sources_prefix
from daimon.core.usage_sweep import settle_finished_turn
from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.tools import ToolResult
from mux.contracts.ids import Scope
from mux.errors import ScopeViolation
from mux.profiles import MANAGED_AGENTS
from pydantic import BaseModel, Field, JsonValue
from pydantic.json_schema import SkipJsonSchema
from sqlalchemy.exc import SQLAlchemyError

from mcp.types import ImageContent, TextContent

log = structlog.get_logger(__name__)

OnRead = Callable[[BetaManagedAgentsSession], Awaitable[None]]
"""Sees a session the tool just read (`_settle_when_finished`)."""

# Each transcript page costs two upstream calls (ownership retrieve + events
# list), so an uncapped walk on a long session can outlast any client timeout.
_MAX_EVENT_PAGES = 20


def _turn_boundary(sent: BetaManagedAgentsSendSessionEvents) -> str:
    """Read the turn boundary event id off a send response.

    The accepted event's id IS the boundary a caller drops through: read
    events at or after the seam's own pre-send clock reading and discard
    everything up to and including this id. The timestamp is NOT read off
    the echo — a live probe against the API showed the send response always
    echoes the event with ``processed_at=None``; the timestamp only appears
    roughly half a second later, once the agent starts on the event — so the
    boundary's clock is the caller's own, captured immediately before the
    send (see the callers' ``now`` parameter). Raises when the send accepted
    nothing — a send with no accepted event is not a started turn and must
    not return a half-answer.
    """
    if not sent.data:
        raise ToolError("send returned no accepted event")
    return sent.data[0].id


async def _send_billed(
    runtime: McpRuntime, session: BetaManagedAgentsSession, message: str, *, scope: Scope
) -> str:
    """Send a headless turn's message with its settle request written first.

    Nothing meters a headless turn live, so its calls are billed by
    `usage_sweep.settle_finished_turn` once a status or transcript read sees
    the turn ended, or by the scheduler drain of the requests no read cleared.
    The request is committed before the send, so an accepted send always has
    one. A send Managed Agents rejected (a 4xx, or nothing accepted) withdraws
    it, restoring any request it replaced; after an error that may hide an
    accepted send (a timeout, a 5xx) it stays. Naming the accepted message is
    best effort: an unnamed request waits on the first message processed after
    it was written. Returns the message's event id (the turn boundary).

    The sources rule goes in front as on the routine runner: the agent chat
    gate turns built-ins, search included, off (`capability_policy`), so it
    is empty unless that policy changes.
    """
    session_id = session.id
    searches = capability_policy("mcp_chat").allow_builtins and claude_searches(
        [tool.model_dump(mode="json") for tool in session.agent.tools]
    )
    text = sources_prefix(on=searches) + message
    async with runtime.session_factory.begin() as db:
        previous, requested_at = await session_settle_state.request_settle(
            db, managed_session_id=session_id
        )
    try:
        # One attempt: an SDK retry would resend after this send's gate.
        sent = await send_session_events(
            single_attempt(runtime),
            session_id,
            scope=scope,
            events=[{"type": "user.message", "content": [{"type": "text", "text": text}]}],
        )
        turn_event_id = _turn_boundary(sent)
    except (ToolError, anthropic.APIStatusError) as err:
        if isinstance(err, anthropic.APIStatusError) and err.status_code >= 500:
            raise
        async with runtime.session_factory.begin() as db:
            await session_settle_state.withdraw_settle_request(
                db, managed_session_id=session_id, requested_at=requested_at, previous=previous
            )
        raise
    try:
        async with runtime.session_factory.begin() as db:
            await session_settle_state.name_settle_turn(
                db,
                managed_session_id=session_id,
                requested_at=requested_at,
                turn_event_id=turn_event_id,
            )
    except SQLAlchemyError:
        # Named boundary: the turn is running and its request stands.
        log.exception("billing.settle_request_unnamed", managed_session_id=session_id)
    return turn_event_id


async def _keep_question(
    runtime: McpRuntime,
    auth: AuthIdentity,
    session: BetaManagedAgentsSession,
    *,
    turn_event_id: str,
    message: str,
) -> KeptQuestion | None:
    """Keep the sent message when its agent is monitored over MCP; the settle that sees
    the turn end keeps the reply (`usage_sweep.settle_finished_turn`)."""
    return await capture_question(
        runtime.session_factory,
        runtime.settings,
        tenant_id=auth.tenant_id,
        agent_id=derive_agent_uuid(tenant_id=auth.tenant_id, ma_agent_id=session.agent.id),
        platform="mcp",
        account_id=auth.account_id,
        conversation=session.id,
        turn_ref=turn_event_id,
        content=message,
    )


def _turn_handle(
    handle: str, turn_event_id: str, started_at: dt.datetime, kept: KeptQuestion | None = None
) -> dict[str, str]:
    """A started turn's handle; `monitoring` is a notice the client shows the person."""
    out = {
        "handle": handle,
        "turn_event_id": turn_event_id,
        "turn_started_at": started_at.isoformat(),
    }
    if kept is not None and kept.notice is not None:
        out["monitoring"] = kept.notice.text
    return out


def _settle_when_finished(runtime: McpRuntime) -> OnRead:
    """Bill a headless turn the moment a read sees it ended (bounded, synchronous)."""

    async def on_read(session: BetaManagedAgentsSession) -> None:
        try:
            await settle_finished_turn(
                runtime.client,
                runtime.session_factory,
                session,
                markup=runtime.settings.billing.markup,
            )
        except Exception:
            # Named boundary: the caller asked for status or a transcript, which
            # billing must never fail. The settle request stays for the next read.
            log.exception("billing.settle_failed", managed_session_id=session.id)

    return on_read


class AgentDescription(BaseModel):
    """Read-only description of a Daimon agent, surfaced for coding agents."""

    model_config = {"frozen": True}

    name: str
    role_summary: str
    skill_names: list[str]
    repo_url: str | None
    environment_name: str | None
    platform: str | None = None
    workspace_id: str | None = None
    workspace: str | None = None
    # Set while the agent is monitored: tell the person before they ask anything.
    monitoring: str | None = None


class AskResult(BaseModel):
    """Final text, chart capabilities, and resumable handle returned by ``ask``."""

    model_config = {"frozen": True}

    handle: str
    message: str
    chart_urls: tuple[ChartUrl, ...] = ()
    # Set when the person must be told this conversation is monitored; show it to them.
    monitoring: str | None = None
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
    notice = [TextContent(type="text", text=result.monitoring)] if result.monitoring else []
    return ToolResult(
        content=[*notice, TextContent(type="text", text=result.message), *result.image_blocks],
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

    An MCP call has no channel of its own, so the cascade falls through
    tenant -> ``runtime.deployment_default``, except for an agent key minted
    in a channel (``token_channel_id``), which resolves as a turn there does.
    This is the same shared ``resolve()`` the Discord adapter uses, so there is
    no second tenant-row-only resolution path.
    """
    async with runtime.session_factory() as session:
        resolved = await resolve(
            session,
            context=ScopeContext(
                tenant_id=auth.tenant_id,
                account_id=auth.account_id,
                channel_id=token_channel_id(auth),
            ),
            default=runtime.deployment_default,
        )
    return resolved.environment_name


_SEALED_SINCE_START_MSG = (
    "this conversation started before its channel's readers were limited, so it can't "
    "take messages under that rule. Start a new one with start_turn. Tell the caller."
)

_CHANNEL_BUDGET_SPENT_MSG = (
    "TERMINAL ERROR: this conversation's channel has used its spending budget. "
    "An admin can raise or clear it."
)

# How long a follow-up waits for a resumed paused turn to end before its send.
_RESUME_WAIT_S = 90.0

_RESUMED_STILL_RUNNING_MSG = (
    "this conversation was paused on tool confirmations; they are answered and the "
    "reply is running again, so your message was not sent. Poll get_session "
    "until idle, then call continue_turn again."
)

_RESUME_FAILED_MSG = (
    "this conversation is paused on tool confirmations and could not be resumed. "
    "Call continue_turn again."
)
_AGENT_CHANGED_MSG = (
    "This agent changed since this conversation started. Start a new one with "
    "start_turn to use the new setup. Nothing was sent. Tell the caller."
)

_MESSAGE_REQUIRED_MSG = (
    "a message is required: only a conversation paused on tool confirmations "
    "can be resumed without one."
)

# The longest one `await_turn` call holds its request open: under the five minutes
# after which common hosts cut a request (Cloud Run's default).
AWAIT_TURN_MAX_S = 240.0
# What one call may take over its wait for the reads and the resume around it. A
# call that outlives both returns "running", inside the five minutes a host allows.
_AWAIT_GRACE_S = 30.0
# After a stream event says a turn ended, how often and how many times `ask` reads a
# session status that still says running before it goes back to waiting.
_SETTLE_TRIES = 10
_SETTLE_GAP_S = 0.3
# What ends an `await_turn` wait: the agent said something, or the turn stopped.
_TURN_NEWS: frozenset[str] = TURN_STOPPED | {"agent.message"}

_STILL_RUNNING_HINT = "Daimon is still working. Call await_turn again to keep waiting."
_PAUSED_UNREAD_HINT = "Daimon stopped. Call await_turn without wait_seconds=0 to let it go on."
_NEW_MESSAGES_HINT = "Daimon is still working. Call await_turn again with this cursor as `after`."
_ENDED_SILENT_HINT = "The reply ended without a message."
_TERMINATED_HINT = (
    "This conversation has ended and takes no more messages. Start a new one with start_turn."
)
_DECLINED_MSG = "Daimon's AI declined this request; rephrase it and ask again with handle {handle}"

_SEAL_CHANGED_DURING_START_MSG = (
    "this conversation's channel rule changed while it was starting. Call start_turn again."
)


async def _bound_seal(runtime: McpRuntime, auth: AuthIdentity) -> frozenset[str]:
    """The seal a channel-bound key's turn runs under now: its channel, when sealed.

    Read right before the session is created or continued, as `reauthorize`
    reads a seal added since admission for a platform turn.
    """
    return (await _bound_posture(runtime, auth, agent_names=()))[0]


async def _bound_posture(
    runtime: McpRuntime, auth: AuthIdentity, *, agent_names: tuple[str | None, ...]
) -> tuple[frozenset[str], bool]:
    """A channel-bound key's seal now, and whether its session's memory is read-only.

    Memory is read-only under the seal unless the agent is the isolated
    channel's own (`memory_writable`), as at chat admission.
    """
    # A provider turn's seals ride its credential (`child_seal_ids`); they
    # keep memory read-only like a channel seal does.
    carried = child_seal_ids(None, auth.seal_ids)
    channel_id = token_channel_id(auth)
    if channel_id is None:
        return carried, bool(carried)
    policy = await load_channel_policy(runtime, auth)
    here = channel_permissions(policy, channel_id=channel_id)
    if here.readers == "any":
        return carried, bool(carried)
    agent = agent_permissions(policy, agent_names)
    return carried | {channel_id}, bool(carried) or not memory_writable(agent, here)


async def _budget_channel(
    runtime: McpRuntime, auth: AuthIdentity, *, agent_names: tuple[str | None, ...]
) -> str | None:
    """The channel a turn is charged to: the isolated channel whose own agent this
    is, wherever an exempt caller runs it, else a key's bound channel."""
    policy = await load_channel_policy(runtime, auth)
    return agent_permissions(policy, agent_names).budget_channel or token_channel_id(auth)


async def _verify_agent_owns_session(
    runtime: McpRuntime,
    auth: AuthIdentity,
    handle: str,
) -> BetaManagedAgentsSession:
    """Assert the caller's agent AND account own the session, not merely its tenant.

    Stricter than ``_verify_tenant_owns_session``: re-derives the session
    agent's UUID and compares it to ``auth.agent_id`` (the verified derived
    per-agent UUID), rejecting sibling-agent handles within the same tenant.
    It also requires the session's ``daimon_account`` tag to be the
    caller's account: every member of a workspace talks to the same agent, and
    a member's session runs with that member's vault, so an agent match alone
    would let one member read, drive or stop another's conversation. Raises
    ``ToolError("conversation not found")`` — same message as a genuinely missing
    session, so existence isn't leaked across agents or accounts.

    A conversation that ran in a sealed channel is then refused outright: these
    callers (agent keys, the hub) run outside every channel, and its transcript
    holds what the seal keeps in. An agent key minted in that channel is inside
    it (``token_channel_id``). Checked on every call, so a follow-up after the
    channel is sealed is refused too.
    """
    try:
        s = await retrieve_session(runtime.client, handle, scope=mcp_scope(auth))
    except ScopeViolation:
        raise ToolError("conversation not found") from None
    derived = derive_agent_uuid(tenant_id=auth.tenant_id, ma_agent_id=str(s.agent.id))
    if auth.agent_id is None or derived != auth.agent_id:
        raise ToolError("conversation not found")
    if not _owned_by_caller(s, auth) and s.id not in await admin_readable_legacy_sessions(
        runtime, auth, [s]
    ):
        raise ToolError("conversation not found")
    await require_session_outside_seals(runtime, auth, s)
    return s


_CHAT_TURN_REFUSED = (
    "a chat reply can't open or drive another conversation through agent chat. "
    "Answer in this conversation instead. Do not retry."
)


def require_outside_chat_turn(auth: AuthIdentity) -> None:
    """Refuse agent-chat turns driven by a chat turn's own credential.

    These tools are for headless callers (agent keys, the hub), which run
    outside every channel. A chat turn's token carries ``chat_agent_id`` and
    is kept off this surface by visibility; this check holds the line if that
    ever changes. A chat turn in a sealed channel could otherwise open or
    continue a session with no seal stamp and carry sealed content into it.
    """
    if auth.chat_agent_id is not None or auth.slack_turn_context_id is not None:
        raise ToolError(_CHAT_TURN_REFUSED)


def _owned_by_caller(session: BetaManagedAgentsSession, auth: AuthIdentity) -> bool:
    """True when ``session`` was created for the caller's account.

    ``create_session`` / ``create_isolated_session`` tag every session with
    ``daimon_account``; an untagged session belongs to no caller.
    """
    return session_belongs_to_caller(session, auth)


async def _resolve_ma_agent(
    runtime: McpRuntime,
    auth: AuthIdentity,
) -> BetaManagedAgentsAgent:
    """Resolve the MA agent for this caller from the verified claim.

    Matches ``auth.agent_id`` (the derived per-agent UUID) against the
    tenant's live agents. Raises ToolError if no match — fails closed (never
    falls back to a broader scope).
    """
    if auth.agent_id is None:
        raise ToolError("agent not found")
    agent = await find_agent_by_derived_uuid_cached(
        runtime.client,
        runtime.resolver_cache,
        tenant_id=auth.tenant_id,
        agent_id=auth.agent_id,
        records=runtime.session_factory,
    )
    if agent is None:
        raise ToolError("agent not found")
    return agent


async def _refuse_data_agent(runtime: McpRuntime, auth: AuthIdentity, ma_agent_id: str) -> None:
    """Agent chat never drives a data-reading agent, nor any agent from a conversation that
    read data (`daimon.core.data_reading`)."""
    if auth.chat_session_id is not None and data_sources_on(runtime.settings):
        async with runtime.session_factory() as session:
            if await has_read(
                session, tenant_id=auth.tenant_id, chat_session_id=auth.chat_session_id
            ):
                raise ToolError(AFTER_DATA_REFUSAL)
    place = FeaturePlace.of(
        auth.tenant_id, auth.platform or "mcp", auth.bound_channel_id, agent_id=ma_agent_id
    )
    if await reads_data(runtime.session_factory, runtime.settings, place):
        raise ToolError(AGENT_REQUEST_REFUSAL)


def agent_pin_names(agent: BetaManagedAgentsAgent) -> tuple[str | None, ...]:
    """Every name a channel pin on ``agent`` may be keyed by (`core.agent_pins`)."""
    return core_agent_pin_names(agent.name, agent.metadata)


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
        grants = await list_live_grant_repositories(
            session, tenant_id=auth.tenant_id, agent_id=auth.agent_id
        )
    repo_url = next((repo.repo_full_name for grant, repo in grants if grant.is_working_repo), None)
    skill_names = [sk.skill_id for sk in ma_agent.skills]
    role_summary = (ma_agent.system or "")[:200]
    notice = await monitored_notice(
        runtime.session_factory,
        runtime.settings,
        tenant_id=auth.tenant_id,
        agent_id=derive_agent_uuid(tenant_id=auth.tenant_id, ma_agent_id=ma_agent.id),
        platform="mcp",
    )
    return AgentDescription(
        name=ma_agent.name,
        role_summary=role_summary,
        skill_names=skill_names,
        repo_url=repo_url,
        environment_name=env_name,
        platform=auth.platform,
        workspace_id=auth.external_id,
        monitoring=notice.text if notice is not None else None,
    )


async def _refuse_another_provider(
    runtime: McpRuntime,
    auth: AuthIdentity,
    *,
    ma_agent_id: str,
    name: str,
    nothing: str = "Nothing was started.",
) -> None:
    """Agent keys and the hub open Managed Agents sessions, so an agent whose backend
    is another provider is refused plainly here, never answered (and billed) on the
    Claude record its definition is saved with. A session opened before a switch
    doesn't continue on Claude either."""
    from daimon.core.stores import agent_backends
    from daimon.core.turn.notices import provider_name
    from mux.contracts.config import DEFAULT_BACKEND

    async with runtime.session_factory() as session:
        revision = await agent_backends.current(
            session,
            auth.tenant_id,
            derive_agent_uuid(tenant_id=auth.tenant_id, ma_agent_id=ma_agent_id),
        )
    backend = revision.backend if revision is not None else None
    if backend is not None and backend != DEFAULT_BACKEND:
        raise ToolError(
            f"{name} runs on {provider_name(backend)}; agent keys and the hub can only "
            f"talk to agents on Claude for now. Ask it in chat instead. {nothing}"
        )


def _canonical_setup(value: JsonValue) -> str:
    """`value` encoded with lists unordered and every `permission_policy` dropped.

    A send's gate rewrites permission policies from the current policy (`gate`),
    and the order of servers, skills, tools or a toolset's per-tool configs
    means nothing to the provider, so neither counts as a change.
    """
    if isinstance(value, dict):
        kept: dict[str, JsonValue] = {
            key: _canonical_setup(item) for key, item in value.items() if key != "permission_policy"
        }
        return canonical_json(kept)
    if isinstance(value, list):
        members: list[JsonValue] = [*sorted(_canonical_setup(item) for item in value)]
        return canonical_json(members)
    return canonical_json(value)


_DESCRIPTIVE_METADATA = frozenset(
    {
        MA_METADATA_KEY_NAME,
        MA_METADATA_KEY_SOURCE_RELEASE,
        MA_METADATA_KEY_SOURCE_RELEASE_AT,
        MA_METADATA_KEY_SOURCE_RELEASE_URL,
        MA_METADATA_KEY_SPEC_HASH,
    }
)
"""Agent metadata that only labels the agent. Any other key may shape a session
(`daimon_isolated` decides which mounts it gets), so a change to it counts."""


def _session_setup(agent: BetaManagedAgentsAgent) -> str:
    """What an agent definition gives a session: all of it but its name, description
    and descriptive metadata (`_DESCRIPTIVE_METADATA`)."""
    multiagent = getattr(agent, "multiagent", None)
    return _canonical_setup(
        {
            "metadata": {
                key: value
                for key, value in agent.metadata.items()
                if key not in _DESCRIPTIVE_METADATA
            },
            "model": agent.model.id,
            "speed": getattr(agent.model, "speed", None) or "standard",
            "system": agent.system,
            "skills": [skill.model_dump(mode="json") for skill in agent.skills],
            "mcp_servers": [server.model_dump(mode="json") for server in agent.mcp_servers],
            "tools": [tool.model_dump(mode="json") for tool in agent.tools],
            "multiagent": None if multiagent is None else multiagent.model_dump(mode="json"),
        }
    )


async def _refuse_a_changed_agent(
    runtime: McpRuntime, auth: AuthIdentity, session: BetaManagedAgentsSession
) -> None:
    """Refuse a message to a session started on an agent version since edited.

    The edit counts when it changed the agent's model, prompt, skills, MCP
    servers, tools or session-shaping metadata (`_session_setup`), read from
    the version the session started on and the current one.

    The comparison is of the agent's own definition, not of what each
    caller's session gets from it: rebuilding a session's past view from
    today's sign-ins can hide a change. So an edit to a server only someone
    else signs in to, or to a custom tool the unattended gate removes anyway,
    asks this caller for a new conversation too: a needless refusal, never a
    missed change.

    A live session can't take any of these safely (new servers need the
    sign-in vault `start_turn` mounts, and a tool change would reach the
    provider only after this turn's safety gate), and agent chat does not
    move a conversation, so the caller starts a new one (`_AGENT_CHANGED_MSG`).
    An unedited agent costs one agent read.
    """
    scope = mcp_scope(auth)
    agent = await retrieve_agent(
        runtime.client, session.agent.id, scope=scope, records=runtime.session_factory
    )
    if agent.version == session.agent.version:
        return
    started = await retrieve_agent_version(
        runtime.client,
        agent.id,
        session.agent.version,
        scope=scope,
        records=runtime.session_factory,
    )
    if _session_setup(started) != _session_setup(agent):
        log.info(
            "agent_chat.agent_changed_refused",
            session_id=session.id,
            from_version=session.agent.version,
            to_version=agent.version,
        )
        raise ToolError(_AGENT_CHANGED_MSG)


async def _mirror_shared_mcp_tokens(
    runtime: McpRuntime,
    auth: AuthIdentity,
    session: BetaManagedAgentsSession,
    *,
    now: Callable[[], dt.datetime],
) -> None:
    """Copy the agent's shared MCP tokens into the caller's mounted vault.

    The chat path does this on every turn (`RemirrorVaultCredentials`), so a
    token someone replaced or revoked reaches an open conversation. GitHub's
    own MCP token is left out, as `create_session` does in App mode.
    """
    mcp = runtime.settings.mcp
    if (
        runtime.fernet is None
        or mcp.public_url is None
        or mcp.jwt_secret is None
        or not session.vault_ids
    ):
        return
    assert auth.agent_id is not None  # _verify_agent_owns_session requires it
    await sync_agent_mcp_credentials(
        runtime.client,
        sessionmaker=runtime.session_factory,
        fernet=runtime.fernet,
        tenant_id=auth.tenant_id,
        agent_id=auth.agent_id,
        account_id=auth.account_id,
        jwt_secret=mcp.jwt_secret.get_secret_value().encode(),
        public_url=str(mcp.public_url),
        now=now(),
        exclude_github_copilot=True,
        vault_ids=runtime.resolver_cache.mcp_vault_ids,
    )


def _agent_chat_binding(runtime: McpRuntime, handle: str) -> str | None:
    """The agent-chat session a relay vault is bound to, only while a server may be
    sent its driver: off, the relay credential stays exactly as it was."""
    return handle if runtime.settings.mcp_identity.names_agent_chat_requester else None


async def _bind_relay_vault(
    runtime: McpRuntime,
    auth: AuthIdentity,
    session: BetaManagedAgentsSession,
    *,
    now: Callable[[], dt.datetime],
) -> None:
    """Bind a new session's relay vault to it (`refresh_relay_vault`), before its
    first send. A continue binds it with its own refresh, older sessions included."""
    mcp = runtime.settings.mcp
    agent_chat = _agent_chat_binding(runtime, session.id)
    if agent_chat is None or mcp.public_url is None or mcp.jwt_secret is None:
        return
    assert auth.agent_id is not None  # _resolve_ma_agent requires it
    public_url = str(mcp.public_url)
    relay_urls = identity_relay_urls(
        (server.url for server in session.agent.mcp_servers), public_url=public_url
    )
    if not relay_urls:
        return
    for vault_id in dict.fromkeys(session.vault_ids):
        await refresh_relay_vault(
            runtime.client,
            vault_id=vault_id,
            account_id=auth.account_id,
            agent_id=auth.agent_id,
            jwt_secret=mcp.jwt_secret.get_secret_value().encode(),
            relay_urls=relay_urls,
            now=now(),
            session_factory=runtime.session_factory,
            scope=mcp_scope(auth),
            agent_chat=agent_chat,
        )


@observed_agent_turn
async def _start_turn_impl(
    runtime: McpRuntime,
    auth: AuthIdentity,
    message: str,
    bundle: str | None = None,
    *,
    now: Callable[[], dt.datetime] = lambda: dt.datetime.now(dt.UTC),
    recheck: Callable[[], Awaitable[None]] | None = None,
    personal_memory: bool = False,
) -> dict[str, str]:
    """Create a new MA session for the caller's agent and send the first message.

    Returns ``{"handle": session_id, "turn_event_id": ..., "turn_started_at": ...}``.
    ``turn_event_id`` is the accepted ``user.message`` event's id, taken from
    the ``events.send`` response. ``turn_started_at`` is ``now()`` captured
    immediately BEFORE that call, not a timestamp read off the response — the
    echo carries no usable timestamp of its own (see ``_turn_boundary``). A
    caller reading the transcript should ask ``list_events`` for events with
    ``created_at_gte=turn_started_at`` and discard everything up to and
    including ``turn_event_id`` — this is the turn boundary.

    Environment resolution (fail-closed):
    - Resolve ``environment_name`` via the shared channel/tenant/deployment
      cascade (``resolve()``, the same as Discord).
    - Look it up on MA via ``find_environment_by_daimon_tag``.
    - Raise ``ToolError("environment not found")`` if either step fails.

    When ``bundle`` is given, the session is created on the isolated path
    (``create_isolated_session``) with the bundle mounted as its only
    resource, instead of the normal vault/repo/env-mounted ``create_session``
    path. See ``bundle_handle`` for the verification steps.

    A caller with no platform user (an internal or CLI token, which ``_admit``
    lets through ungated and unbilled) gets a session stamped
    ``daimon_billing_exempt="mcp-internal-caller"``, so the usage sweep skips
    it: the operator absorbs that usage. The stamp is the creator's posture
    and covers the whole session, including later ``continue_turn`` calls.

    An isolated channel's own agent is charged to that channel
    (``_budget_channel``), from the hub too. An agent key minted in a channel
    (``token_channel_id``) opens the session as a turn in that channel would:
    stamped with the channel as its origin and otherwise its budget channel,
    sealed when the channel is, with read-only memory unless the agent is that
    isolated channel's own.

    A chat turn's own credential is refused (``require_outside_chat_turn``).

    ``personal_memory`` mounts the caller's own personal memory store beside
    the shared one. Only a caller whose identity is a verified person passes
    it: the hub, whose identity is the person's platform OAuth login and whose
    session handles only that account can use (``hub._verify_account_owns_session``).
    An agent key, an operator or CLI token, and an internal caller leave it
    off: a key is the agent, its sessions are the agent's, and whoever holds
    it is not established as one person.

    ``recheck`` (``_ctx._admission_recheck``) runs the access decision again
    immediately before the session is created and again before the message
    is sent, so a pin or allowlist change saved during the lookups above
    stops the turn rather than only the next one.
    """
    require_outside_chat_turn(auth)
    ma_agent = await _resolve_ma_agent(runtime, auth)
    await _refuse_data_agent(runtime, auth, ma_agent.id)
    await _refuse_another_provider(
        runtime,
        auth,
        ma_agent_id=ma_agent.id,
        name=ma_agent.metadata.get(MA_METADATA_KEY_NAME) or ma_agent.name,
    )
    billing_exempt: ExemptReason | None = (
        "mcp-internal-caller" if auth.platform_user_id is None else None
    )

    env_name = await _resolve_environment_name(runtime, auth)
    if env_name is None:
        raise ToolError("environment not found")

    environment: BetaEnvironment | None = await find_environment_by_daimon_tag(
        runtime.client, tenant_id=auth.tenant_id, name=env_name, records=runtime.session_factory
    )
    if environment is None:
        raise ToolError("environment not found")

    is_isolated = ma_agent.metadata.get(MA_METADATA_KEY_ISOLATED) == "true"
    channel_id = token_channel_id(auth)
    budget_channel_id = await _budget_channel(runtime, auth, agent_names=agent_pin_names(ma_agent))

    if bundle is not None:
        if not is_isolated:
            raise ToolError("bundle requires an isolated agent")
        if runtime.settings.mcp.jwt_secret is None:
            raise ToolError("bundle upload is not set up")
        claims = (
            bundle_handle.verify(
                runtime.settings.mcp.jwt_secret.get_secret_value(),
                bundle,
                tenant_id=auth.tenant_id,
                agent_id=auth.agent_id,
                now=dt.datetime.now(dt.UTC),
            )
            if auth.agent_id is not None
            else None
        )
        if claims is None:
            raise ToolError("bundle not found")
        try:
            await retrieve_file_metadata(runtime.client, claims.file_id, scope=mcp_scope(auth))
        except anthropic.NotFoundError as err:
            raise ToolError("bundle expired; re-upload") from err
        resources: list[Resource] = [
            {"type": "file", "file_id": claims.file_id, "mount_path": "/bundle.tar.gz"}
        ]
        if recheck is not None:
            await recheck()
        seal, memory_read_only = await _bound_posture(
            runtime, auth, agent_names=agent_pin_names(ma_agent)
        )
        session = await create_isolated_session(
            runtime.client,
            agent=ma_agent,
            environment=environment,
            account_id=auth.account_id,
            tenant_id=auth.tenant_id,
            resources=resources,
            tool_safety=runtime.settings.tool_safety,
            billing_exempt=billing_exempt,
            agent_chat=True,
            memory_read_only=memory_read_only,
            budget_channel_id=budget_channel_id,
            origin_channel_id=channel_id,
            origin_seal_ids=seal,
        )
    else:
        github_fallback_pat: str | None = None
        github_app_id: str | None = runtime.settings.github_app.app_id
        github_app_private_key: str | None = (
            runtime.settings.github_app.private_key.get_secret_value()
            if runtime.settings.github_app.private_key is not None
            else None
        )

        if recheck is not None:
            await recheck()
        seal, memory_read_only = await _bound_posture(
            runtime, auth, agent_names=agent_pin_names(ma_agent)
        )

        async def before_create() -> None:
            if recheck is not None:
                await recheck()
            # The stamp and memory mount were decided on the seal read above;
            # a seal change during the work below would leave them stale.
            if await _bound_seal(runtime, auth) != seal:
                raise ToolError(_SEAL_CHANGED_DURING_START_MSG)

        toolset, identity = await place_session(
            runtime.session_factory,
            runtime.settings,
            FeaturePlace.of(
                auth.tenant_id,
                auth.platform or "mcp",
                channel_id,
                agent_id=ma_agent.id,
                backend=MANAGED_AGENTS.provider,
            ),
            ma_agent,
            server_setup=True,
        )
        session = await create_session(
            runtime.client,
            agent=ma_agent,
            environment=environment,
            toolset=toolset,
            identity=identity,
            mcp_settings=runtime.settings.mcp,
            account_id=auth.account_id,
            tenant_id=auth.tenant_id,
            agent_uuid=auth.agent_id,
            session_factory=runtime.session_factory,
            fernet=runtime.fernet,
            app_session_unmapped=True,
            tool_safety=runtime.settings.tool_safety,
            legacy_memory_servers=toolset.legacy_memory_servers(
                get_repo_memory_settings(runtime.settings).legacy_servers
            ),
            github_fallback_pat=github_fallback_pat,
            github_app_id=github_app_id,
            github_app_private_key=github_app_private_key,
            agent_github_app=runtime.settings.github_app,
            github_mcp_router=runtime.settings.github.mcp_router,
            billing_exempt=billing_exempt,
            agent_chat=True,
            memory_read_only=memory_read_only,
            budget_channel_id=budget_channel_id,
            origin_channel_id=channel_id,
            origin_seal_ids=seal,
            personal_memory=personal_memory,
            # Vault, repo-token and env work run first: decide again right
            # before the session exists, so a pin or seal landing meanwhile
            # leaves none.
            before_create=before_create,
        )

    if (observation := current_outcome.get()) is not None:
        observation.session_id = session.id
        observation.agent_id = str(ma_agent.id)
    # Fenced as a continue is: a send on this session by another caller, once its
    # handle is listed, cannot fall between this driver record and this send.
    async with session_mutation_fence(runtime.session_factory, session.id):
        if recheck is not None:
            await recheck()
        await gate(runtime, auth, session)
        await _bind_relay_vault(runtime, auth, session, now=now)
        send = await record_driver(
            runtime.session_factory,
            managed_session_id=session.id,
            signed_in_person=auth.signed_in_person,
            session_status=None,
            earlier=await earlier_turn_open(runtime, auth, session.id, new_session=True),
            provenance=session_provenance(session.metadata),
            new_session=True,
        )
        turn_started_at = now()
        turn_event_id = await _send_billed(runtime, session, message, scope=mcp_scope(auth))
        kept = await _keep_question(
            runtime, auth, session, turn_event_id=turn_event_id, message=message
        )
        channel = rules_channel(auth, session)
        await record_turn(
            runtime, session.id, turn_event_id=turn_event_id, rules_channel_id=channel, send=send
        )
    watch_turn(runtime, auth, session.id, turn_event_id=turn_event_id, rules_channel_id=channel)

    return _turn_handle(session.id, turn_event_id, turn_started_at, kept)


@observed_agent_turn
async def _continue_turn_impl(
    runtime: McpRuntime,
    auth: AuthIdentity,
    handle: str,
    message: str | None,
    *,
    now: Callable[[], dt.datetime] = lambda: dt.datetime.now(dt.UTC),
    recheck: Callable[[], Awaitable[None]] | None = None,
) -> dict[str, str]:
    """Send a follow-up message on an existing session.

    Before the send the session is re-gated from the current tool policy
    (``_agent_chat_safety.gate``). A session paused on tool confirmations
    nobody is answering (after a restart) is resumed first: its owner's call
    answers the paused turn's calls from the current policy. With no
    ``message`` that is all it does, and the paused turn's boundary is
    returned. With one, the message is sent once the resumed turn ends, which
    is waited for up to ``_RESUME_WAIT_S``; past that the call is refused and
    the resumed turn keeps running.

    Returns ``{"handle": handle, "turn_event_id": ..., "turn_started_at": ...}``,
    the same three-key shape as ``start_turn``. ``turn_event_id`` is taken
    from THIS call's ``events.send`` response, not the session's earlier
    events; ``turn_started_at`` is ``now()`` captured immediately BEFORE that
    send (see ``_start_turn_impl`` for why the echo's own timestamp isn't
    used). ``_verify_agent_owns_session`` guards against cross-tenant AND
    same-tenant cross-agent handles (Tampering threat mitigation). A
    chat turn's own credential is refused (``require_outside_chat_turn``).
    Only a session opened through agent chat can be mutated here; a platform
    conversation never lends its attended execution grant to this caller.
    ``recheck`` runs the access decision again immediately before the send.

    A channel-bound key's channel sealed since the session opened refuses the
    follow-up: the session's memory is writable and its stamp lacks the seal,
    so sealed messages would outlive an unseal (`_bound_seal`). The session's
    own budget channel, which a billed follow-up is charged to, is gated too.
    """
    require_outside_chat_turn(auth)
    session = await _verify_agent_owns_session(runtime, auth, handle)
    require_session_driver(session, auth)
    await _refuse_data_agent(runtime, auth, str(session.agent.id))
    if (observation := current_outcome.get()) is not None:
        observation.session_id = handle
        observation.agent_id = str(session.agent.id)
    # Switched since this session opened: it would go on answering on Claude.
    await _refuse_another_provider(
        runtime,
        auth,
        ma_agent_id=session.agent.id,
        name=session.agent.name,
        nothing="Nothing was sent.",
    )

    async def ready() -> BetaManagedAgentsSession:
        """Every check a send needs, then the gate, right before the send."""
        if recheck is not None:
            await recheck()
        # Read inside the fence: the gate rewrites this snapshot.
        current = await _verify_agent_owns_session(runtime, auth, handle)
        require_session_driver(current, auth)
        if not await _bound_seal(runtime, auth) <= seal_ids(current.metadata):
            raise ToolError(_SEALED_SINCE_START_MSG)
        if current.archived_at is not None:
            raise ToolError("This conversation is archived.")
        await _gate_origin_spend(
            auth,
            sessionmaker=runtime.session_factory,
            markup=runtime.settings.billing.markup,
            tool_name="continue_turn",
            channel_id=(current.metadata or {}).get(MA_METADATA_KEY_BUDGET_CHANNEL),
            agent_id=current.agent.id,
            budget_message=_CHANNEL_BUDGET_SPENT_MSG,
        )
        async with runtime.session_factory.begin() as db:
            touched = await touch_unmapped_app_session(db, session_id=handle)
            if touched is False:
                raise ToolError("This conversation is closed.")
        # A chat turn's bind reconciles here (`prepare_session_for_turn`). Agent
        # chat refuses instead; a resume alone finishes the turn already running.
        if message is not None:
            await _refuse_a_changed_agent(runtime, auth, current)
        await _mirror_shared_mcp_tokens(runtime, auth, current, now=now)
        mcp = runtime.settings.mcp
        if mcp.public_url is not None and mcp.jwt_secret is not None:
            assert auth.agent_id is not None  # verified above, including after fence acquisition
            public_url = str(mcp.public_url)
            await refresh_mounted_mcp_vaults(
                runtime.client,
                vault_ids=current.vault_ids,
                account_id=auth.account_id,
                agent_id=auth.agent_id,
                jwt_secret=mcp.jwt_secret.get_secret_value().encode(),
                public_url=public_url,
                now=now(),
                session_factory=runtime.session_factory,
                scope=mcp_scope(auth),
                relay_urls=identity_relay_urls(
                    (server.url for server in current.agent.mcp_servers), public_url=public_url
                ),
                agent_chat=_agent_chat_binding(runtime, handle),
            )
        await gate(runtime, auth, current)
        return current

    async with session_mutation_fence(runtime.session_factory, handle):
        # The fence may have waited behind a transfer or another send.
        current = await ready()
        await record_driver(
            runtime.session_factory,
            managed_session_id=handle,
            signed_in_person=auth.signed_in_person,
            session_status=current.status,
            earlier=UNREAD,
            provenance=session_provenance(current.metadata),
            resuming=True,
        )
        resumed = await resume_paused(runtime, auth, current)
        if resumed is not None:
            if message is None:
                return {
                    "handle": handle,
                    "turn_event_id": resumed.turn_event_id,
                    "turn_started_at": now().isoformat(),
                }
            try:
                ended = await asyncio.wait_for(asyncio.shield(resumed.worker), _RESUME_WAIT_S)
            except TimeoutError:
                raise ToolError(_RESUMED_STILL_RUNNING_MSG) from None
            if not ended:
                raise ToolError(_RESUME_FAILED_MSG)
            # The wait may have outlived a policy, rule, access or budget
            # change: decide and gate again for the new message.
            current = await ready()
        elif message is None:
            raise ToolError(_MESSAGE_REQUIRED_MSG)
        send = await record_driver(
            runtime.session_factory,
            managed_session_id=handle,
            signed_in_person=auth.signed_in_person,
            session_status=current.status,
            earlier=await earlier_turn_open(runtime, auth, handle, new_session=False),
            provenance=session_provenance(current.metadata),
        )
        turn_started_at = now()
        # Inside the fence, so settle requests follow the order of the sends.
        turn_event_id = await _send_billed(runtime, current, message, scope=mcp_scope(auth))
        kept = await _keep_question(
            runtime, auth, current, turn_event_id=turn_event_id, message=message
        )
        channel = rules_channel(auth, current)
        await record_turn(
            runtime, handle, turn_event_id=turn_event_id, rules_channel_id=channel, send=send
        )
        watch_turn(runtime, auth, handle, turn_event_id=turn_event_id, rules_channel_id=channel)
    return _turn_handle(handle, turn_event_id, turn_started_at, kept)


async def _list_sessions_impl(
    runtime: McpRuntime,
    auth: AuthIdentity,
) -> list[SessionInfo]:
    """List the sessions THIS caller started with its agent.

    Resolves the caller's MA agent from the verified claim, lists that agent's
    sessions, and keeps only those tagged with the caller's account. Other
    agents' sessions in the same tenant, other members' sessions of this
    agent, and conversations that ran in a sealed channel are never returned.
    """
    ma_agent = await _resolve_ma_agent(runtime, auth)
    owned: list[BetaManagedAgentsSession] = []
    async for s in walk_sessions(runtime.client, agent_id=str(ma_agent.id), scope=mcp_scope(auth)):
        if _owned_by_caller(s, auth):
            owned.append(s)
    return [SessionInfo.from_ma(s) for s in await sessions_outside_seals(runtime, auth, owned)]


async def _get_session_impl(
    runtime: McpRuntime,
    auth: AuthIdentity,
    handle: str,
    *,
    on_read: OnRead | None = None,
) -> SessionInfo:
    """Return one session's status/metadata (no reply text).

    ``_verify_agent_owns_session`` guards cross-tenant AND same-tenant
    cross-agent handles. ``on_read`` (the tool's
    ``_settle_when_finished``) sees the session read.

    A turn whose tool confirmations this process is answering reads as
    ``running``; one paused on confirmations nobody is answering reads as
    ``paused`` with a hint to resume it. Either way the turn has not ended,
    so ``on_read`` does not see it. This is a read: it answers nothing.
    """
    s = await _verify_agent_owns_session(runtime, auth, handle)
    info = SessionInfo.from_ma(s)
    paused = await turn_status(runtime, auth, s)
    if paused is not None:
        status, hint = paused
        return info.model_copy(update={"status": status, "hint": hint})
    if on_read is not None:
        await on_read(s)
    return info


async def _archive_my_session_impl(
    runtime: McpRuntime,
    auth: AuthIdentity,
    handle: str,
) -> dict[str, str]:
    """Archive a finished session so it stops being a live session forever.

    Without this, every reader thread accumulates as a live MA session and
    the usage sweep re-reads all of them on every tick. Not a cancel — a
    running turn is stopped with the cancel tool, not this one.
    ``_verify_agent_owns_session`` guards cross-tenant AND same-tenant
    cross-agent handles.
    """
    require_outside_chat_turn(auth)
    mounted = await _verify_agent_owns_session(runtime, auth, handle)
    require_session_driver(mounted, auth)
    async with session_mutation_fence(runtime.session_factory, handle):
        require_session_driver(await _verify_agent_owns_session(runtime, auth, handle), auth)
        await archive_session_record(runtime.client, handle, scope=mcp_scope(auth))
    try:
        await archive_relay_vaults(runtime.client, mounted.vault_ids, scope=mcp_scope(auth))
    except Exception:
        # The scheduler's janitor archives it once no live session mounts it.
        log.exception("mcp.agent_chat.relay_vault_cleanup_failed", session_id=handle)
    try:
        await close_headless_app_session(
            runtime.client,
            runtime.session_factory,
            session_id=handle,
            fernet=runtime.fernet,
        )
    except Exception:
        # The durable vault row stays pending for the scheduler's next sweep.
        log.exception("mcp.agent_chat.app_session_cleanup_failed", session_id=handle)
    return {"handle": handle, "archived": "true"}


async def _cancel_turn_impl(
    runtime: McpRuntime,
    auth: AuthIdentity,
    handle: str,
) -> dict[str, str]:
    """Stop a running turn with one unconditional interrupt, then report status.

    Sends ``user.interrupt`` unconditionally — no read-then-send: a status
    read before the send would race the session's own state (it can change
    between the read and the send), and the interrupt is safe to send at any
    status anyway, including an already-idle or already-terminated session
    (harmless no-op). This call returns immediately; it does not wait for the
    interrupt to take effect — poll ``get_my_session`` to see the session
    reach ``idle`` or ``terminated`` (contrast the turn codec's
    ``interrupt``, which sends the same event and then blocks on the SSE
    stream for a terminal ``session.status_idle``; this tool is
    fire-and-return, not fire-and-wait).

    An interrupted turn is still billed for what it already consumed before
    the interrupt landed — the model work already done is real work and is
    not refunded. The report host surfaces that to the reader.

    ``_verify_agent_owns_session`` guards cross-tenant AND same-tenant
    cross-agent handles before any send.
    """
    require_outside_chat_turn(auth)
    require_session_driver(await _verify_agent_owns_session(runtime, auth, handle), auth)
    async with session_mutation_fence(runtime.session_factory, handle):
        require_session_driver(await _verify_agent_owns_session(runtime, auth, handle), auth)
        await send_session_events(
            runtime.client, handle, events=[{"type": "user.interrupt"}], scope=mcp_scope(auth)
        )
    session = await retrieve_session(runtime.client, handle, scope=mcp_scope(auth))
    return {"handle": handle, "status": session.status}


async def _get_turn_cost_impl(
    runtime: McpRuntime,
    auth: AuthIdentity,
    handle: str,
    turn_started_at: str,
    turn_event_id: str,
) -> dict[str, object]:
    """Fold one finished turn's model-request cost — the RAW PROVIDER COST
    BEFORE MARKUP.

    This is NOT expected to equal the tenant ledger's debit for the same
    turn: the ledger applies ``settings.billing.markup`` on top of this
    figure (``daimon.core.tenant_balance.debit_amount``). An integrator who
    assumes the two match will build a reconciliation that never balances.

    Pass the ``turn_started_at``/``turn_event_id`` returned by ``start_turn``
    or ``continue_turn``. Pages ``span.model_request_end`` events at or after
    ``turn_started_at`` and folds each into
    ``Decimal(str(cost_of(event.model_usage, pricing))).quantize(Decimal("0.000001"))``,
    summed. The boundary event itself (``turn_event_id``) is never folded in,
    even if it were ever echoed back by this filtered listing.

    Returns ``{"cost_usd": <str(Decimal) or None>, "event_count": <int>}``.
    ``cost_usd`` is serialised as a string, never a float — a float
    round-trip is exactly the drift ``debit_amount``'s ``Decimal(str(...))``
    conversion exists to avoid. ``cost_usd`` is ``None`` (NOT zero) when the
    session's model has no pricing row: a zero would falsely claim the turn
    was free. A turn with pricing but no model-request events yet returns a
    real zero cost, distinguishable from the unpriced ``None`` case.

    ``_verify_agent_owns_session`` guards cross-tenant AND same-tenant
    cross-agent handles; the session it returns is reused for the
    pricing lookup rather than retrieved twice.
    """
    session = await _verify_agent_owns_session(runtime, auth, handle)
    model_id = session.agent.model.id
    pricing = MODEL_PRICING.get(model_id)
    if pricing is None:
        log.warning("agent_chat.get_turn_cost.unpriced_model", handle=handle, model_id=model_id)

    total = Decimal("0")
    event_count = 0
    page: str | None = None
    for _ in range(_MAX_EVENT_PAGES):
        list_kwargs: dict[str, Any] = {
            "created_at_gte": turn_started_at,
            "types": ["span.model_request_end"],
            "order": "asc",
        }
        if page is not None:
            list_kwargs["page"] = page
        cursor = await list_events_page(
            runtime.client, handle, query=list_kwargs, scope=mcp_scope(auth)
        )
        for event in cursor.data:
            if event.id == turn_event_id:
                continue
            if event.type != "span.model_request_end":
                continue
            event_count += 1
            if pricing is None:
                continue
            cost = cost_of(event.model_usage, pricing)
            if cost is None:
                continue
            total += Decimal(str(cost)).quantize(Decimal("0.000001"))
        if cursor.next_page is None:
            break
        page = cursor.next_page

    return {
        "cost_usd": str(total) if pricing is not None else None,
        "event_count": event_count,
    }


async def _require_live_result_access(runtime: McpRuntime, auth: AuthIdentity) -> None:
    """A private Discord result requires current membership, even after a long turn."""
    if auth.platform == "discord" and auth.platform_user_id is not None:
        await _policy_gate(
            auth,
            sessionmaker=runtime.session_factory,
            membership_resolver=runtime.membership_resolver,
            tool_name="list_events",
            agent_names=None,
            pin_exempt=False,
            require_fresh=True,
        )


async def _list_events_impl(
    runtime: McpRuntime,
    auth: AuthIdentity,
    handle: str,
    page: str | None,
    limit: int | None,
    order: Literal["asc", "desc"] | None,
    created_at_gte: str | None = None,
    types: list[str] | None = None,
    *,
    on_read: OnRead | None = None,
) -> Page[SessionEventOut]:
    """List a session's events (the transcript) for the caller to read.

    This is how a primitives-only caller reads the agent's reply: fold the
    ``agent.message`` events client-side. ``_verify_agent_owns_session``
    guards cross-tenant AND same-tenant cross-agent handles.

    ``created_at_gte`` and ``types`` let a poller read one turn instead of
    the whole transcript: pass the ``turn_started_at`` from ``start_turn``/
    ``continue_turn`` as ``created_at_gte`` and
    ``types=["agent.message", "session.status_idle"]`` to see only this
    turn's reply and completion signal. ``on_read`` is as for ``_get_session_impl``.
    """
    session = await _verify_agent_owns_session(runtime, auth, handle)
    if on_read is not None:
        await on_read(session)
    list_kwargs: dict[str, Any] = {}
    if page is not None:
        list_kwargs["page"] = page
    if limit is not None:
        list_kwargs["limit"] = limit
    if order is not None:
        list_kwargs["order"] = order
    if created_at_gte is not None:
        list_kwargs["created_at_gte"] = created_at_gte
    if types is not None:
        list_kwargs["types"] = types
    cursor = await list_events_page(
        runtime.client, handle, query=list_kwargs, scope=mcp_scope(auth)
    )
    await _require_live_result_access(runtime, auth)
    return Page[SessionEventOut](
        items=[SessionEventOut.from_event(event) for event in cursor.data],
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
            f"conversation {handle} is {session.status!r}; deliver charts only after "
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
        raise ToolError(f"no finished reply found for conversation {handle}")
    if turn_started_at is None:
        raise ToolError(f"no start of the finished reply found for conversation {handle}")

    result = await _delivered_reply(
        runtime, auth, handle, turn_started_at=turn_started_at, final_text=final_text
    )
    await _require_live_result_access(runtime, auth)
    return result


OnTick = Callable[[], Awaitable[None]]
"""Called about every `RECHECK_S` while a call waits, to tell its client it is alive."""


async def _wait_for_news(
    feed: TurnFeed,
    kinds: Collection[str],
    *,
    worker: asyncio.Task[bool] | None,
    seconds: float,
) -> Woke | None:
    """Sleep until the session has news, and at most `seconds` or `RECHECK_S`.

    News is an event in `kinds`, which is returned, a stream that went quiet or
    closed, or the end of this process's confirmation `worker` (a worker that gives
    up leaves the turn paused without any event). The caller reads the session's
    state after every return, news or none: a wait never trusts the stream alone,
    so an event it missed costs at most `RECHECK_S`.
    """
    return await feed.next(kinds, within=min(seconds, RECHECK_S), or_done=worker)


class TurnNews(BaseModel):
    """What ``await_turn`` saw: where the turn stands and what the agent said."""

    model_config = {"frozen": True}

    handle: str
    # "running", "idle" (the turn ended), "paused" or "terminated". Plain str: the
    # provider may report a status this release does not know; it counts as running.
    status: str
    # What the agent said since `after`, oldest first. With status "idle" the last
    # one is the final answer, also when an earlier call already returned it.
    messages: tuple[str, ...] = ()
    # Pass back as `after` on the next call.
    cursor: str | None = None
    # What to do next, in words for the person.
    hint: str | None = None
    # The tool call running now and for how long, when one is.
    activity: str | None = None
    chart_urls: tuple[ChartUrl, ...] = ()
    image_blocks: SkipJsonSchema[tuple[ImageContent, ...]] = Field(
        default=(), exclude=True, repr=False
    )


def _turn_news_result(news: TurnNews) -> ToolResult:
    """Prose for the model first (messages, then the hint), the typed payload beside it."""
    lines = [*news.messages, *([news.hint] if news.hint else [])]
    return ToolResult(
        content=[
            *(TextContent(type="text", text=line) for line in lines),
            *news.image_blocks,
        ],
        structured_content=news.model_dump(mode="json"),
    )


@dataclass(frozen=True)
class _NewestTurn:
    """The newest turn of a session, read from its event list."""

    boundary_id: str | None
    # When the agent took the message up; None while it is still queued.
    started_at: dt.datetime | None
    # (event id, text) of each agent message, oldest first.
    said: tuple[tuple[str, str], ...]
    # An idle that is not a pause was processed after the message was taken up.
    ended: bool
    refused: bool

    def said_after(self, cursor: str | None) -> tuple[tuple[str, str], ...]:
        """The messages after `cursor`; all of them when it names none of this turn's."""
        ids = [event_id for event_id, _ in self.said]
        return self.said[ids.index(cursor) + 1 :] if cursor in ids else self.said


_NOT_LISTED = _NewestTurn(None, None, (), ended=False, refused=False)


async def _newest_turn(
    runtime: McpRuntime, auth: AuthIdentity, handle: str, *, expected: str | None
) -> _NewestTurn:
    """Read the session's newest turn back to its ``user.message`` boundary.

    `expected` is the boundary agent chat recorded for its latest send. While the
    list still ends at an older boundary, that send is not listed yet and nothing
    of the older turn is returned: its idle and its reply belong to the turn before.
    A message queued behind a running turn is listed before that turn's last events,
    so list order alone does not show whose they are. Only an agent message or an
    idle processed after this turn's own message was counts as this turn's.
    """
    said: list[tuple[dt.datetime | None, str, str]] = []
    idle: SessionEventOut | None = None
    page: str | None = None
    for _ in range(_MAX_EVENT_PAGES):
        events = await _list_events_impl(
            runtime,
            auth,
            handle,
            page,
            100,
            "desc",
            types=["user.message", "agent.message", "session.status_idle"],
        )
        for event in events.items:
            if event.type == "user.message":
                if expected is not None and event.id != expected:
                    return _NOT_LISTED
                started_at = _event_timestamp(event)
                if started_at is None:
                    # Still queued: whatever is listed after it is the turn before.
                    return _NewestTurn(event.id, None, (), ended=False, refused=False)
                extra: dict[str, Any] = (idle.model_extra or {}) if idle is not None else {}
                stopped_at = _event_timestamp(idle) if idle is not None else None
                stop: object = extra.get("stop_reason")
                paused = not isinstance(stop, dict) or (
                    cast("dict[str, object]", stop).get("type") == "requires_action"
                )
                ended = not paused and stopped_at is not None and stopped_at > started_at
                return _NewestTurn(
                    boundary_id=event.id,
                    started_at=started_at,
                    # A message queued behind a running turn is listed before that
                    # turn's last words: only what was processed after it is this turn's.
                    said=tuple(
                        (event_id, text)
                        for at, event_id, text in reversed(said)
                        if at is not None and at > started_at
                    ),
                    ended=ended,
                    refused=ended and refusal_error(extra.get("stop_details")) is not None,
                )
            if event.type == "session.status_idle":
                # Newest first: only the newest idle says how the turn stands.
                idle = idle or event
            elif (text := _agent_message_text(event)) is not None:
                said.append((_event_timestamp(event), event.id, text))
        if events.next_page is None:
            break
        page = events.next_page
    return _NOT_LISTED


_TOOL_USES = frozenset({"agent.tool_use", "agent.mcp_tool_use", "agent.custom_tool_use"})


async def _activity(runtime: McpRuntime, auth: AuthIdentity, handle: str) -> str | None:
    """The tool call running now and for how long, from the newest few events.

    Best effort, for a caller that wants to say what a long turn is doing: any
    failure reads as nothing to say. The caller's access was checked by the gated
    transcript read this follows.
    """
    try:
        newest = await list_events_page(
            runtime.client, handle, query={"order": "desc", "limit": 10}, scope=mcp_scope(auth)
        )
        finished: set[str] = set()
        for event in newest.data:
            raw = event.model_dump(mode="json")
            for key in ("tool_use_id", "mcp_tool_use_id", "custom_tool_use_id"):
                if isinstance(raw.get(key), str):
                    finished.add(raw[key])
            if event.type not in _TOOL_USES:
                continue
            if event.id in finished:
                return None
            started_at = _event_timestamp(SessionEventOut.from_event(event))
            name = " ".join(part for part in (raw.get("mcp_server_name"), raw.get("name")) if part)
            if not name:
                return None
            if started_at is None:
                return name
            seconds = max(0, int((dt.datetime.now(dt.UTC) - started_at).total_seconds()))
            return f"{name}, running for {seconds} seconds"
    except Exception:
        # Named boundary: what the turn is doing is a courtesy; the wait's answer stands.
        log.info("mcp.agent_chat.activity_unread", session_id=handle)
    return None


async def _delivered_reply(
    runtime: McpRuntime,
    auth: AuthIdentity,
    handle: str,
    *,
    turn_started_at: dt.datetime,
    final_text: str,
) -> AskResult:
    """A finished turn's reply with its charts, delivered once per turn boundary."""
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


def _news(
    handle: str,
    new: tuple[tuple[str, str], ...],
    after: str | None,
    status: str,
    hint: str | None = None,
    *,
    activity: str | None = None,
) -> TurnNews:
    """A result that carries the unseen messages and moves the cursor past them."""
    return TurnNews(
        handle=handle,
        status=status,
        messages=tuple(text for _, text in new),
        cursor=new[-1][0] if new else after,
        hint=hint,
        activity=activity,
    )


async def _await_turn_impl(
    runtime: McpRuntime,
    auth: AuthIdentity,
    handle: str,
    *,
    after: str | None = None,
    wait_seconds: float = AWAIT_TURN_MAX_S,
    resume: Callable[[], Awaitable[object]] | None = None,
    on_read: OnRead | None = None,
    on_tick: OnTick | None = None,
    feed: TurnFeed | None = None,
    clock: Callable[[], float] = monotonic,
) -> TurnNews:
    """Wait on the session's event stream until its newest turn has news.

    Returns at the first of: the agent said something since `after`; the turn
    ended; it is paused and cannot go on; the session is terminated; or
    `wait_seconds` passed (at most `AWAIT_TURN_MAX_S`). The session is read once,
    then again when the stream brings an event, and at least every `RECHECK_S`.

    A turn agent chat started that is paused with no worker answering it (the
    service restarted, or the worker reached the unattended run limit) is resumed
    once through `resume`, the owner's admitted ``continue_turn`` without a message,
    which also starts this process's worker. So the caller that waits keeps the turn
    going, in a request that holds CPU for as long as it waits. If the resume is
    refused, or the resumed turn stops again, the result is ``paused`` and says why.

    With `wait_seconds` 0 this is a read: it opens no stream, resumes nothing and
    answers no tool call. The whole call, reads and resume included, is bounded a
    little past its wait (`_AWAIT_GRACE_S`); past that it returns ``running``.
    """
    bound = max(0.0, min(wait_seconds, AWAIT_TURN_MAX_S))
    feed = feed or TurnFeed(runtime, auth, handle)
    try:
        async with asyncio.timeout(bound + _AWAIT_GRACE_S):
            return await _await_turn(
                runtime,
                auth,
                handle,
                after=after,
                bound=bound,
                resume=resume if bound > 0 else None,
                on_read=on_read,
                on_tick=on_tick,
                feed=feed,
                clock=clock,
            )
    except TimeoutError:
        return TurnNews(handle=handle, status="running", cursor=after, hint=_STILL_RUNNING_HINT)
    finally:
        await feed.close()


async def _await_turn(
    runtime: McpRuntime,
    auth: AuthIdentity,
    handle: str,
    *,
    after: str | None,
    bound: float,
    resume: Callable[[], Awaitable[object]] | None,
    on_read: OnRead | None,
    on_tick: OnTick | None,
    feed: TurnFeed,
    clock: Callable[[], float],
) -> TurnNews:
    """`_await_turn_impl`'s loop: read the turn, return on news, else sleep on `feed`."""
    deadline = clock() + bound
    resumed = False
    woke: Woke | None = None
    while True:
        if bound > 0:
            # Subscribed before the state is read, so no event falls between them.
            await feed.open()
        # The state is read from the event list, once it shows the event that woke us.
        await caught_up(runtime, auth, handle, woke)
        current = await _get_session_impl(runtime, auth, handle, on_read=on_read)
        turn = await _newest_turn(
            runtime, auth, handle, expected=await recorded_turn_id(runtime, handle)
        )
        new = turn.said_after(after)
        news = partial(_news, handle, new, after)

        # The turn is read after the status, so it is the later word: a turn the
        # worker finished between the two reads has ended, whatever the status said.
        if turn.ended:
            if turn.refused:
                raise ToolError(_DECLINED_MSG.format(handle=handle))
            if not turn.said or turn.started_at is None:
                return news("idle", _ENDED_SILENT_HINT)
            final_id, final_text = turn.said[-1]
            reply = await _delivered_reply(
                runtime, auth, handle, turn_started_at=turn.started_at, final_text=final_text
            )
            earlier = tuple(text for event_id, text in new if event_id != final_id)
            return TurnNews(
                handle=handle,
                status="idle",
                messages=(*earlier, reply.message),
                cursor=final_id,
                chart_urls=reply.chart_urls,
                image_blocks=reply.image_blocks,
            )
        if current.status == "terminated":
            return news("terminated", _TERMINATED_HINT)
        if current.status == "paused":
            if current.hint != PAUSED_HINT:
                return news("paused", current.hint)
            if bound == 0:
                return news("paused", _PAUSED_UNREAD_HINT)
            if resume is not None and not resumed:
                resumed = True
                try:
                    await resume()
                except ToolError as err:
                    # Answered meanwhile by a worker elsewhere: nothing left to resume.
                    if (await _get_session_impl(runtime, auth, handle)).status != "paused":
                        continue
                    return news("paused", f"Daimon stopped and could not go on: {err}")
                continue
            why = why_stopped(runtime, handle)
            return news(
                "paused",
                f"Daimon stopped: {why}. Call await_turn again to let it go on."
                if why is not None
                else "Daimon stopped. Call await_turn again to let it go on.",
            )
        if new:
            return news(
                "running", _NEW_MESSAGES_HINT, activity=await _activity(runtime, auth, handle)
            )
        remaining = deadline - clock()
        if remaining <= 0:
            return news(
                "running", _STILL_RUNNING_HINT, activity=await _activity(runtime, auth, handle)
            )
        woke = await _wait_for_news(
            feed, _TURN_NEWS, worker=local_worker(runtime, handle), seconds=remaining
        )
        if woke is None and on_tick is not None:
            await on_tick()


@observed_agent_turn
async def _ask_impl(
    runtime: McpRuntime,
    auth: AuthIdentity,
    message: str,
    *,
    handle: str | None = None,
    timeout_seconds: float = 120.0,
    clock: Callable[[], float] = monotonic,
    feed: TurnFeed | None = None,
    now: Callable[[], dt.datetime] = lambda: dt.datetime.now(dt.UTC),
    recheck: Callable[[], Awaitable[None]] | None = None,
    personal_memory: bool = False,
    on_read: OnRead | None = None,
) -> AskResult:
    """Start a turn (or continue one given ``handle``), wait boundedly for idle, and
    return its final reply.

    The wait is on the session's event stream (``TurnFeed``), not a poll every
    second: the session is read after the send, then again when the stream says the
    turn stopped, and at least every ``RECHECK_S``. The stream is opened before that
    first read, so a turn that ends in between is not missed.

    The reply is read from THIS turn's side of the boundary the send
    returned. That matters most on the resume path: a resumable session is
    already idle, and the agent only picks up the new message a moment after
    the send is acknowledged, so an unfiltered "newest agent.message" read on
    the first poll would hand back the previous turn's answer.
    """
    if handle is None:
        started = await _start_turn_impl(
            runtime, auth, message, now=now, recheck=recheck, personal_memory=personal_memory
        )
    else:
        started = await _continue_turn_impl(
            runtime, auth, handle, message, now=now, recheck=recheck
        )
    handle = started["handle"]
    if (observation := current_outcome.get()) is not None:
        observation.session_id = handle
    turn_event_id = started["turn_event_id"]
    turn_started_at = dt.datetime.fromisoformat(started["turn_started_at"])
    deadline = clock() + timeout_seconds
    feed = feed or TurnFeed(runtime, auth, handle)
    woke: Woke | None = None
    try:
        while True:
            await feed.open()
            await caught_up(runtime, auth, handle, woke)
            current = await _get_session_impl(runtime, auth, handle, on_read=on_read)
            if woke is not None and woke.stop not in (None, "requires_action"):
                # The stream says the turn ended; the status read can trail it a moment.
                for _ in range(_SETTLE_TRIES):
                    if current.status != "running":
                        break
                    await asyncio.sleep(_SETTLE_GAP_S)
                    current = await _get_session_impl(runtime, auth, handle, on_read=on_read)
            # Only terminated is treated as terminal: the status value set is
            # upstream-controlled (see SessionInfo.status), so an unmodeled
            # transient status keeps waiting until the deadline instead of
            # failing a turn that admission already billed.
            if current.status == "paused":
                raise ToolError(
                    f"Daimon turn on handle {handle} is {current.hint}"
                    + (" or wait with await_turn" if current.hint == PAUSED_HINT else "")
                )
            if current.status == "terminated":
                if (observation := current_outcome.get()) is not None:
                    observation.finish(reason=TerminationReason.SESSION_TERMINATED)
                raise ToolError(
                    f"The reply reached final status {current.status!r}; "
                    f"resume or inspect events with handle {handle}"
                )
            if current.status == "idle":
                page: str | None = None
                final_text: str | None = None
                # The newest idle decides: a refusal ends the turn whatever text came before it.
                refused: bool | None = None
                for _ in range(_MAX_EVENT_PAGES):
                    events = await _list_events_impl(
                        runtime,
                        auth,
                        handle,
                        page,
                        100,
                        "desc",
                        created_at_gte=turn_started_at.isoformat(),
                        types=["agent.message", "session.status_idle"],
                    )
                    for event in events.items:
                        if event.id == turn_event_id:
                            continue
                        if event.type == "session.status_idle":
                            if refused is None:
                                details = (event.model_extra or {}).get("stop_details")
                                refused = refusal_error(details) is not None
                            continue
                        final_text = _agent_message_text(event)
                        if final_text is not None:
                            break
                    if final_text is not None or events.next_page is None:
                        break
                    page = events.next_page

                if refused:
                    if (observation := current_outcome.get()) is not None:
                        observation.finish(reason=TerminationReason.REFUSED)
                    raise ToolError(_DECLINED_MSG.format(handle=handle))
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
                    if (observation := current_outcome.get()) is not None:
                        observation.finish(reason=TerminationReason.COMPLETED)
                    await _require_live_result_access(runtime, auth)
                    return AskResult(
                        handle=handle,
                        message=delivery.message,
                        chart_urls=delivery.chart_urls,
                        image_blocks=delivery.image_blocks,
                        monitoring=started.get("monitoring"),
                    )

            remaining = deadline - clock()
            if remaining <= 0:
                if (observation := current_outcome.get()) is not None:
                    observation.finish(reason=TerminationReason.CEILING)
                notice = started.get("monitoring")
                raise ToolError(
                    f"Daimon did not finish a reply within "
                    f"{timeout_seconds:g} seconds and is still working; wait for it with "
                    f"await_turn and handle {handle}" + (f"\n{notice}" if notice else "")
                )
            woke = await _wait_for_news(
                feed, TURN_STOPPED, worker=local_worker(runtime, handle), seconds=remaining
            )
    finally:
        await feed.close()


def still_waiting(ctx: Context) -> OnTick:
    """Tell a waiting call's client it is alive, with an MCP progress notification.

    A client that asked for progress shows it, and the bytes keep a proxy from
    cutting a connection it takes for idle. Without a progress token this sends
    nothing.
    """
    started = monotonic()

    async def on_tick() -> None:
        with contextlib.suppress(Exception):
            await ctx.report_progress(
                progress=monotonic() - started,
                total=AWAIT_TURN_MAX_S,
                message="Daimon is still working",
            )

    return on_tick


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
    before creating a session or sending an event, including the operator's
    channel pin: a pinned agent is refused here, since an MCP turn runs in no
    channel, unless the key was minted in one of its channels. The policy half
    of that gate runs again right before each session create and message send
    (``_admission_recheck``). Every other tool —
    including ``cancel_turn`` and ``get_turn_cost`` — stays on bare ``_auth``;
    ``deliver_turn_charts`` is the one exception that performs a bounded
    artifact-store write when optional link delivery is configured.
    """

    async def pin_names(auth: AuthIdentity) -> tuple[str | None, ...]:
        return agent_pin_names(await _resolve_ma_agent(runtime, auth))

    def recheck_for(auth: AuthIdentity, tool_name: str) -> Callable[[], Awaitable[None]]:
        async def names() -> tuple[str | None, ...]:
            return await pin_names(auth)

        return _admission_recheck(
            auth,
            sessionmaker=runtime.session_factory,
            membership_resolver=runtime.membership_resolver,
            tool_name=tool_name,
            agent_names=names,
        )

    @mcp.tool(tags={"agent-chat"})  # pyright: ignore[reportArgumentType]
    async def describe_agent(ctx: Context) -> AgentDescription:  # pyright: ignore[reportUnusedFunction]
        """Describe the agent associated with this MCP token.

        Returns the agent's name, system-prompt summary, skill identifiers,
        and configured environment. No parameters — agent identity is read
        from the token claim (confused-deputy mitigation). ``monitoring``, when
        set, is a notice to show the person before they ask anything.
        """
        return await _describe_agent_impl(runtime, await _auth(ctx))

    @mcp.tool(tags={"agent-chat"}, name="list_my_sessions")  # pyright: ignore[reportArgumentType]
    async def list_my_sessions(ctx: Context) -> list[SessionInfo]:  # pyright: ignore[reportUnusedFunction]
        """List the sessions you started with this agent (id, status, title, timestamps).

        ``id`` is the handle you pass to ``get_session``, ``list_events``,
        ``deliver_turn_charts``, and ``continue_turn``. Scoped to this agent
        and to the account the token was minted for. No parameters — identity
        is read from the token claim.
        """
        return await _list_sessions_impl(runtime, await _auth(ctx))

    @mcp.tool(tags={"agent-chat"})  # pyright: ignore[reportArgumentType]
    async def start_turn(  # pyright: ignore[reportUnusedFunction]
        ctx: Context, message: str, bundle: str | None = None
    ) -> dict[str, str]:
        """Start a new conversation turn with the agent and return a handle.

        Creates a persistent MA session with vault/repo/env parity and sends
        the first user message. Returns ``{"handle": "<session_id>",
        "turn_event_id": "<event_id>", "turn_started_at": "<iso8601>"}``.
        ``handle`` is passed to ``await_turn``, ``get_session``,
        ``list_events``, and ``continue_turn``. Wait for the reply with
        ``await_turn``: it returns when the agent says something or the turn
        ends, so there is nothing to poll. ``turn_event_id`` and
        ``turn_started_at`` identify this turn's first event, for a caller that
        reads ``list_events`` itself: pass ``created_at_gte=turn_started_at``
        and discard everything up to and including ``turn_event_id``.
        ``monitoring``, when present, is a notice to show the person.

        ``bundle`` is an opaque handle returned by the bundle upload route.
        Passing it mounts that archive read-only in the session's uploads
        directory instead of the normal vault/repo/env mounts, and requires
        an isolated agent — a non-isolated agent is refused outright with
        the message 'bundle requires an isolated agent'. A handle that fails
        verification for any reason (tampered, expired, wrong tenant, wrong
        agent) is refused with the message 'bundle not found', one wording
        for every cause. A handle whose underlying file is gone is refused
        with the message 'bundle expired; re-upload' — that one specifically
        means push the archive again.
        """
        auth = await _check_admission(
            ctx,
            sessionmaker=runtime.session_factory,
            membership_resolver=runtime.membership_resolver,
            billing_config=billing_config,
            markup=runtime.settings.billing.markup,
            tool_name="start_turn",
            agent_names=pin_names,
        )
        return await _start_turn_impl(
            runtime, auth, message, bundle, recheck=recheck_for(auth, "start_turn")
        )

    @mcp.tool(tags={"agent-chat"})  # pyright: ignore[reportArgumentType]
    async def ask(  # pyright: ignore[reportUnusedFunction]
        ctx: Context, message: str, handle: str | None = None
    ) -> AskResult:
        """Ask one question and wait for the final answer and any chart images. Starts a
        persistent turn, waits up to about 120 seconds for it to end, and
        returns the final text plus a resumable handle. A reply that takes longer
        ends this call with an error naming the handle while the agent keeps
        working: wait for it with ``await_turn``. Callers need current
        membership and invoker-policy access before reply delivery where required. Chart
        images are embedded by default; short-lived download links are added when private
        artifact storage is configured. Pass ``handle`` from a previous result to
        continue that conversation instead of starting a new one. Show the person
        ``monitoring`` when it is set; the text result leads with it.
        Use a handle opened through agent chat. Platform conversations and
        older sessions without headless creation provenance cannot be driven here.
        """
        auth = await _check_admission(
            ctx,
            sessionmaker=runtime.session_factory,
            membership_resolver=runtime.membership_resolver,
            billing_config=billing_config,
            markup=runtime.settings.billing.markup,
            tool_name="ask",
            agent_names=pin_names,
        )
        return _ask_tool_result(  # type: ignore[return-value]
            await _ask_impl(
                runtime,
                auth,
                message,
                handle=handle,
                recheck=recheck_for(auth, "ask"),
                on_read=_settle_when_finished(runtime),
            )
        )

    @mcp.tool(tags={"agent-chat"})  # pyright: ignore[reportArgumentType]
    async def continue_turn(  # pyright: ignore[reportUnusedFunction]
        ctx: Context, handle: str, message: str | None = None
    ) -> dict[str, str]:
        """Send a follow-up message on an existing session, or resume a paused one.

        Use this to continue a multi-turn conversation. Returns
        ``{"handle": "<session_id>", "turn_event_id": "<event_id>",
        "turn_started_at": "<iso8601>"}`` — the same three-key shape as
        ``start_turn``, with the boundary taken from THIS message's send, not
        the session's earlier turns. Wait for the reply with ``await_turn``.
        ``monitoring``, when present, is a notice to show the person.

        When ``get_my_session`` reports ``status`` ``paused`` with the hint
        to call ``continue_turn``, a turn this tool started is waiting on tool
        confirmations nobody is answering (the service restarted). This call
        resumes it, as waiting with ``await_turn`` also does: the waiting calls
        are decided by the current tool policy (reads run; writes are refused
        unless the operator allowed them in unattended runs). ``message`` is
        then optional: without one, the paused turn's boundary is returned;
        with one, it is sent once the resumed turn ends, or the call is refused
        if that takes more than about 90 seconds. A turn waiting on someone's
        approval card in chat is refused: only that card answers it. Any other
        session needs a ``message``.
        Use a handle opened through agent chat. Platform conversations and
        older sessions without headless creation provenance cannot be driven here.
        If the agent's model, prompt, skills, MCP servers or tools changed since
        the session started, a message is refused: start a new one with ``start_turn``.
        """
        auth = await _check_admission(
            ctx,
            sessionmaker=runtime.session_factory,
            membership_resolver=runtime.membership_resolver,
            billing_config=billing_config,
            markup=runtime.settings.billing.markup,
            tool_name="continue_turn",
            agent_names=pin_names,
        )
        return await _continue_turn_impl(
            runtime, auth, handle, message, recheck=recheck_for(auth, "continue_turn")
        )

    @mcp.tool(tags={"agent-chat"})  # pyright: ignore[reportArgumentType]
    async def await_turn(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        handle: str,
        after: str | None = None,
        wait_seconds: float = AWAIT_TURN_MAX_S,
    ) -> TurnNews:
        """Wait for news from the turn running on ``handle``, without polling.

        Call this after ``start_turn`` or ``continue_turn``, and again each time
        it returns ``running``. It returns as soon as one of these happens:

        - the agent said something: ``status`` ``running``, the new text in
          ``messages``. Pass ``cursor`` back as ``after`` on the next call;
        - the turn ended: ``status`` ``idle``. The last of ``messages`` is the
          final answer, with any chart images and links;
        - the turn stopped and cannot go on: ``status`` ``paused``, and ``hint``
          says why and what to do;
        - the conversation is over: ``status`` ``terminated``;
        - ``wait_seconds`` passed (at most about four minutes): ``status``
          ``running`` with no new messages. Call it again.

        A turn paused on tool confirmations nobody is answering is resumed by
        this call, as ``continue_turn`` does: the waiting calls are decided by
        the current tool policy. So a long turn runs to its end while you wait,
        with no ``get_my_session`` loop and no ``continue_turn``. ``activity``,
        when set, names the tool call running now and for how long.
        ``wait_seconds=0`` only reads where the turn stands: it waits for
        nothing and resumes nothing.
        Use a handle opened through agent chat.
        """
        auth = await _auth(ctx)

        async def resume() -> None:
            admitted = await _check_admission(
                ctx,
                sessionmaker=runtime.session_factory,
                membership_resolver=runtime.membership_resolver,
                billing_config=billing_config,
                markup=runtime.settings.billing.markup,
                tool_name="continue_turn",
                agent_names=pin_names,
            )
            await _continue_turn_impl(
                runtime, admitted, handle, None, recheck=recheck_for(admitted, "continue_turn")
            )

        return _turn_news_result(  # type: ignore[return-value]
            await _await_turn_impl(
                runtime,
                auth,
                handle,
                after=after,
                wait_seconds=wait_seconds,
                resume=resume,
                on_read=_settle_when_finished(runtime),
                on_tick=still_waiting(ctx),
            )
        )

    @mcp.tool(tags={"agent-chat"}, name="get_my_session")  # pyright: ignore[reportArgumentType]
    async def get_my_session(ctx: Context, handle: str) -> SessionInfo:  # pyright: ignore[reportUnusedFunction]
        """Get one session's status and metadata (no reply text, read-only).

        A one-time status read. To wait for a turn, use ``await_turn`` instead
        of calling this again and again. ``status`` becomes
        ``idle``/``terminated`` when a turn has finished and the transcript can
        be read with ``list_events``. ``status`` ``paused`` means the turn waits on tool
        confirmations; ``hint`` says whether ``continue_turn`` resumes it or
        it waits for an approval in the chat where it was asked. Reading
        never answers them.
        """
        return await _get_session_impl(
            runtime, await _auth(ctx), handle, on_read=_settle_when_finished(runtime)
        )

    @mcp.tool(tags={"agent-chat"})  # pyright: ignore[reportArgumentType]
    async def archive_my_session(ctx: Context, handle: str) -> dict[str, str]:  # pyright: ignore[reportUnusedFunction]
        """Archive a session when its conversation is finished.

        An archived session is no longer live and stops being re-read by
        usage collection. This is NOT a cancel — to stop a running turn, use
        the cancel tool instead. Returns ``{"handle": "<session_id>",
        "archived": "true"}``.
        Use a handle opened through agent chat. Platform conversations and
        older sessions without headless creation provenance cannot be driven here.
        """
        return await _archive_my_session_impl(runtime, await _auth(ctx), handle)

    @mcp.tool(tags={"agent-chat"})  # pyright: ignore[reportArgumentType]
    async def cancel_turn(ctx: Context, handle: str) -> dict[str, str]:  # pyright: ignore[reportUnusedFunction]
        """Stop a running turn immediately.

        Sends an interrupt regardless of the session's current status —
        sending one to an already-idle session is harmless. Returns
        ``{"handle": "<session_id>", "status": "<observed_status>"}``
        immediately; it does not wait for the interrupt to take effect:
        ``await_turn`` returns once the session reaches ``idle`` or
        ``terminated``. An interrupted turn is still billed for what it
        already consumed before the interrupt landed: the model work already
        done is real work and is not refunded.
        Use a handle opened through agent chat. Platform conversations and
        older sessions without headless creation provenance cannot be driven here.
        """
        return await _cancel_turn_impl(runtime, await _auth(ctx), handle)

    @mcp.tool(tags={"agent-chat"})  # pyright: ignore[reportArgumentType]
    async def get_turn_cost(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        handle: str,
        turn_started_at: str,
        turn_event_id: str,
    ) -> dict[str, object]:
        """Return one finished turn's raw provider cost, before markup.

        Pass the ``turn_started_at``/``turn_event_id`` returned by
        ``start_turn`` or ``continue_turn``. Call this once per terminal —
        the host polls status every two seconds and should read cost once,
        not on every poll. Returns ``{"cost_usd": "<decimal string>" | None,
        "event_count": <int>}``. ``cost_usd`` is ``None`` when the session's
        model has no pricing row (never zero — zero would falsely claim the
        turn was free). This figure is NOT expected to equal the tenant
        ledger's debit for the same turn: the ledger applies
        ``settings.billing.markup`` on top of it.
        """
        return await _get_turn_cost_impl(
            runtime, await _auth(ctx), handle, turn_started_at, turn_event_id
        )

    @mcp.tool(tags={"agent-chat"})  # pyright: ignore[reportArgumentType]
    async def list_events(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        handle: str,
        page: str | None = None,
        limit: int | None = None,
        order: Literal["asc", "desc"] | None = None,
        created_at_gte: str | None = None,
        types: list[str] | None = None,
    ) -> Page[SessionEventOut]:
        """List a session's events — the transcript.

        Callers must still pass current membership and invoker-policy checks where required.

        The agent's reply is in the ``agent.message`` events. Call this once
        ``get_session`` reports the session is idle to read what the agent said.

        A poller reading one turn should pass
        ``types=["agent.message", "session.status_idle"]``,
        ``created_at_gte=<the turn_started_at from start_turn/continue_turn>``,
        and ``order="asc"``, then discard events up to and including the
        ``turn_event_id`` it was given, locally. A turn is finished only when
        a ``session.status_idle`` event appears AFTER the boundary, or
        ``get_my_session`` reports the session ``terminated`` — a bare
        ``idle`` status read right after a send may be the PREVIOUS turn's
        state and must not be trusted. ``rescheduling`` counts as running.
        """
        return await _list_events_impl(
            runtime,
            await _auth(ctx),
            handle,
            page,
            limit,
            order,
            created_at_gte,
            types,
            on_read=_settle_when_finished(runtime),
        )

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
