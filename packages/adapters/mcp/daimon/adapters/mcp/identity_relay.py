"""Relay a session's requests to a signed-identity MCP server.

`<app root>/mcp-identity/<name>` accepts a session's own relay credential
(`daimon.core.mcp_vault.create_relay_vault`), signs who is calling
(`daimon.core.mcp_identity`) and forwards the streamable HTTP request to the
configured server with that token as its bearer. The response, an SSE stream
included, is passed back unchanged. A person's call to one of a server's `wake_tools`
also carries a callback watch link (`_wake_link`) the server posts to when the work ends.
"""

from __future__ import annotations

import ssl
import uuid
from collections.abc import Awaitable, Callable, Iterable, Mapping
from datetime import datetime, timedelta
from typing import cast

import httpx
import structlog
from anthropic import AsyncAnthropic
from daimon.adapters.mcp.auth.verifier import FOREIGN_HOME_CLAIM, TOKEN_KIND_CLAIM
from daimon.adapters.mcp.tools.watches import callback_watch_terms
from daimon.core.access_policy import CHANNEL_SCOPE_PREFIX, DM_SCOPE_PREFIX
from daimon.core.agent_chat_requester import driver_signed_in
from daimon.core.audience_gate import read_audience_mode
from daimon.core.channel_audience import channel_has_outsiders
from daimon.core.config import SignedIdentityServer, load_watches_settings
from daimon.core.continuity.continuation import ContinuationRequest
from daimon.core.defaults.builtin_agent import find_agent_or_builtin
from daimon.core.defaults.metadata import BUILTIN_AGENT_NAMES, MA_METADATA_KEY_MANAGED
from daimon.core.errors import WatchFeatureUnreadable
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.mcp_hold import HOLD_CLAIM, parse_hold_claim
from daimon.core.mcp_identity import (
    DM_ROUTINE_DESTINATION,
    RELAY_CLAIM,
    RELAY_PATH,
    REQUESTER_PLATFORMS,
    UNOPENED_DM_PLACE,
    IdentitySigner,
    RelayBinding,
    RequesterBodyError,
    RequesterKind,
    TurnPlace,
    WakeReason,
    called_tool,
    caller_claims,
    matching_turn,
    origin_turn_place,
    parse_relay_claim,
    read_request_body,
    relay_url,
    requester_conversation,
    routine_conversation,
    routine_destination_key,
    routine_revision,
    stamp_parsed,
    watch_conversation,
)
from daimon.core.mcp_server_url import same_ascii_mcp_url
from daimon.core.ops_catalog import OpsCode
from daimon.core.ops_events import OpsEvent, report_ops_event
from daimon.core.signed_room import SignedRoom, routine_room, turn_room
from daimon.core.stores import watches
from daimon.core.stores.agent_avatars import normalize_agent_name
from daimon.core.stores.domain import (
    ChatPlatform,
    ContinuationReason,
    RoutineRow,
    TaskContinuationRow,
    TurnOriginRow,
    WatchRow,
)
from daimon.core.stores.lead_intake import execution_is_active
from daimon.core.stores.routines import get_routine
from daimon.core.stores.task_continuations import get_running_wake_row
from daimon.core.stores.thread_sessions import get_thread_session_by_id
from daimon.core.stores.turn_origins import get_session_origin, list_active_origins
from daimon.core.stores.watches import get_firing, watch_seed
from daimon.core.turn_origin import origin_place
from daimon.core.workflows.place import BoundStep, bound_step
from fastmcp.server.auth.auth import TokenVerifier
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.background import BackgroundTask
from starlette.requests import Request
from starlette.responses import Response, StreamingResponse

log = structlog.get_logger(__name__)

RELAY_ROUTE = RELAY_PATH + "/{name}"
RELAY_METHODS = ["GET", "POST", "DELETE"]
#: A JSON-RPC request body; anything larger is refused before it is read in full.
MAX_BODY_BYTES = 4 * 1024 * 1024
#: Gap allowed between bytes of a POST answer, a tool call's SSE stream included.
POST_READ_TIMEOUT = 300.0
#: Open connections per server, so one slow server cannot hold every socket.
MAX_CONNECTIONS = 100
#: Wait for a free connection; past it the caller is told to retry (503).
POOL_TIMEOUT = 5.0
_REQUEST_HEADERS = (
    "accept",
    "content-type",
    "last-event-id",
    "mcp-protocol-version",
    "mcp-session-id",
)
_RESPONSE_HEADERS = ("content-type", "mcp-session-id", "mcp-protocol-version", "cache-control")
#: A wake link's watch lasts this long after the latest call that asked for one, never
#: longer than `callback_watch_lifetime` allows: one live per conversation and server,
#: so the owner's watch limit is never filled by old jobs.
WAKE_LIFETIME = timedelta(days=1)


async def _wake_link(
    sessionmaker: async_sessionmaker[AsyncSession],
    origin: TurnOriginRow,
    *,
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
    person: str,
    server: str,
    now: datetime,
) -> str | None:
    """A fresh link to the conversation's wake watch for `server` (`watches.wake_link`);
    None when watches or callbacks are off here, or the watch cannot be made (a helper's
    turn, a Slack DM, the owner's limit). It skips the approval card, as
    `test_a_wake_link_is_made_without_the_approval_card` records. The link is a secret:
    it goes only into the forwarded call, never into a log."""
    settings = load_watches_settings()
    if not settings.enabled or settings.callback_base_url is None:
        return None
    binding = ContinuationRequest(
        tenant_id=tenant_id,
        platform=cast(ChatPlatform, origin.platform),
        parent_channel_id=origin.parent_channel_id,
        thread_id=origin.thread_id,
        requester_account_id=account_id,
        requester_external_user_id=person,
        target_ma_agent_id=origin.responder_ma_agent_id,
        target_name=origin.responder_name,
        requested_work=None,
        reason="watch_fired",
        idempotency_key=uuid.uuid4(),
    )
    try:
        allowed = await callback_watch_terms(sessionmaker, None, origin)
        async with sessionmaker.begin() as db:
            watch_id, token = await watches.wake_link(
                db,
                binding,
                server=server,
                lifetime=WAKE_LIFETIME if allowed is None else min(allowed, WAKE_LIFETIME),
                now=now,
            )
    except Exception as err:  # boundary: the call still runs, only without a wake
        log.info("mcp_identity.wake_link_unavailable", server=server, error_type=type(err).__name__)
        return None
    return f"{settings.callback_base_url}/callbacks/watches/{watch_id}/{token}"


def build_relay_clients(servers: Iterable[SignedIdentityServer]) -> dict[str, httpx.AsyncClient]:
    """One bounded client per server; the caller closes them on shutdown."""
    clients: dict[str, httpx.AsyncClient] = {}
    for server in servers:
        verify: bool | ssl.SSLContext = True
        if server.ca_certificate is not None:
            context = ssl.create_default_context()
            try:
                context.load_verify_locations(cadata=server.ca_certificate)
            except (ssl.SSLError, ValueError) as err:
                raise ValueError("relay CA certificate is invalid") from err
            verify = context
        clients[server.name] = httpx.AsyncClient(
            verify=verify,
            timeout=httpx.Timeout(30.0, read=POST_READ_TIMEOUT, pool=POOL_TIMEOUT),
            limits=httpx.Limits(
                max_connections=MAX_CONNECTIONS, max_keepalive_connections=MAX_CONNECTIONS // 2
            ),
        )
    return clients


async def read_capped_body(request: Request) -> bytes | None:
    """The request body, or None once it passes `MAX_BODY_BYTES`."""
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > MAX_BODY_BYTES:
        return None
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > MAX_BODY_BYTES:
            return None
    return bytes(body)


async def _session_turn(
    sessionmaker: async_sessionmaker[AsyncSession],
    binding: RelayBinding,
    *,
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
    agent_id: uuid.UUID,
    platform: str,
    now: datetime,
) -> tuple[TurnOriginRow | None, bool]:
    """The person's turn the calling session is running now, or None, and whether it was
    found through the credential's own grant.

    The binding names the session's `thread_sessions` row. With the session's
    execution grant, the turn is the one holding it now (`bind_origin_session`),
    so each call names the turn that made it, beside a queued turn or after the
    row was replaced. Without one (a session with no grant) a turn counts only
    while that row is the caller's live session and runs one turn in its thread.
    """
    if binding.session_key is None:
        return None, False
    async with sessionmaker() as session:
        row = await get_thread_session_by_id(session, id=binding.session_key)
        if row is None or (row.tenant_id, row.account_id, row.platform) != (
            tenant_id,
            account_id,
            platform,
        ):
            return None, False
        if binding.grant is not None:
            held = await get_session_origin(
                session,
                tenant_id=tenant_id,
                account_id=account_id,
                platform=platform,
                chat_session_id=binding.grant,
                now=now,
            )
            origins = [] if held is None else [held]
        elif row.status != "live":
            return None, False
        else:
            origins = await list_active_origins(
                session, tenant_id=tenant_id, account_id=account_id, platform=platform, now=now
            )
    turn = matching_turn(origins, tenant_id=tenant_id, agent_id=agent_id, thread_id=row.thread_id)
    return turn, turn is not None and binding.grant is not None


def _live_person_turn(origin: TurnOriginRow, *, now: datetime) -> bool:
    """Whether `origin` is a person's own turn still running: started by their message,
    not a helper's, and not expired. It proves the turn runs, not how old its message is."""
    return (
        origin.human_invocation is True
        and bool(origin.platform_message_id)
        and origin.helper_of is None
        and origin.expires_at > now
    )


async def _thread_wake(
    sessionmaker: async_sessionmaker[AsyncSession],
    origin: TurnOriginRow,
    *,
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
    person: str,
    platform: str,
    now: datetime,
) -> tuple[ContinuationReason, str, SignedRoom] | None:
    """The reason, thread and room kind of the queued continuation `origin` runs, or None.

    A wake's turn takes its claim's owner as its origin id (`turn_origin.wake_turn`), so
    the row is found from the turn alone: claimed by that owner, started and inside its
    lease, for this workspace, platform, requester and agent. Its origin and signed channel
    must be one thread under the row's parent: the row's own thread, or for a watch report
    the new thread its firing made (`stores.watches.watch_seed` rechecks the watch). A turn
    in the channel itself, a DM or channel scope and a helper find none. The room is judged
    as a routine posting there judges it (`routine_room`), never as the turn's own. Reads only.
    """
    thread = origin.thread_id
    if (
        origin.helper_of is not None
        or origin_place(origin) == "dm"
        or thread.startswith((DM_SCOPE_PREFIX, CHANNEL_SCOPE_PREFIX))
        or origin.platform_channel_id != thread
        or thread == origin.parent_channel_id
    ):
        return None
    async with sessionmaker() as session:
        row = await _running_wake(
            session, origin, tenant_id=tenant_id, account_id=account_id, person=person,
            platform=platform, now=now,
        )  # fmt: skip
        if row is None or row.parent_channel_id != origin.parent_channel_id:
            return None
        if row.thread_id != thread and not (
            row.reason == "watch_report"
            and (firing := await get_firing(session, row.idempotency_key)) is not None
            and firing.delivery_thread_id == thread
            and await _watch_runs(session, row.model_copy(update={"thread_id": thread}), now)
        ):
            return None
    destination = {
        "place_type": "thread",
        "channel_id": thread,
        "parent_channel_id": row.parent_channel_id,
    }
    found = await routine_room(
        sessionmaker, tenant_id=tenant_id, platform=platform, destination=destination, now=now
    )
    return None if found is None else (row.reason, found[0], found[1])


async def _running_wake(
    session: AsyncSession,
    origin: TurnOriginRow,
    *,
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
    person: str,
    platform: str,
    now: datetime,
) -> TaskContinuationRow | None:
    """The wake row `origin`'s turn runs, in any thread, for this requester and agent."""
    row = await get_running_wake_row(
        session, tenant_id=tenant_id, platform=platform, thread_id=None,
        owner=origin.id.hex, now=now,
    )  # fmt: skip
    if (
        row is None
        or not row.requester_account_id == origin.account_id == account_id
        or row.requester_external_user_id != person
        or row.target_ma_agent_id != origin.responder_ma_agent_id
    ):
        return None
    return row


async def _watch_runs(session: AsyncSession, row: TaskContinuationRow, now: datetime) -> bool:
    """Whether the watch behind a report still runs it in the place `row` names."""
    try:
        return await watch_seed(session, row, now=now) is not None
    except WatchFeatureUnreadable:
        return False


async def _dm_wake(
    sessionmaker: async_sessionmaker[AsyncSession],
    binding: RelayBinding,
    origin: TurnOriginRow,
    *,
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
    person: str,
    platform: str,
    now: datetime,
) -> bool:
    """Whether `origin` is a watch report's live turn in its owner's fresh DM scope.

    The credential binds only its session, grant and the owner's DM, opened by the report
    before its turn. The turn holds that grant, is no person's and no helper's, and took
    its claim's owner as its id; that claim is a `watch_report` still running under its
    lease for this owner and agent, its watch still reports to the owner's DM
    (`watch_seed`), and the firing recorded this session as the one it runs on.
    """
    if (
        binding.dm_channel is None
        or binding.routine_id is not None
        or binding.session_key is None
        or origin.helper_of is not None
        or origin.human_invocation is not False
        or origin.attended
        or origin.platform != "discord"
        or not origin.thread_id.startswith(DM_SCOPE_PREFIX)
        or origin.parent_channel_id != origin.thread_id
        or origin.platform_channel_id is not None
        or origin.platform_message_id is not None
        or origin.expires_at <= now
    ):
        return False
    async with sessionmaker() as session:
        row = await _running_wake(
            session, origin, tenant_id=tenant_id, account_id=account_id, person=person,
            platform=platform, now=now,
        )  # fmt: skip
        if row is None or row.reason != "watch_report":
            return False
        firing = await get_firing(session, row.idempotency_key)
        watch = None if firing is None else await watches.get_watch(session, firing.watch_id)
        mapping = await get_thread_session_by_id(session, id=binding.session_key)
        return (
            firing is not None
            and watch is not None
            and watch.delivery == "dm"
            and watch.owner_external_id == person
            and mapping is not None
            and firing.delivery_session_id == mapping.ma_session_id
            and await _watch_runs(session, row, now)
        )


async def _routine_run(
    sessionmaker: async_sessionmaker[AsyncSession],
    binding: RelayBinding,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    live: bool = False,
) -> RoutineRow | None:
    """The routine a scheduled run's credential names, or None.

    Only a routine of this tenant that runs this agent (by id) and is still owned by
    the user the run started for counts; a routine since deleted, handed to someone
    else or aimed at another agent names nobody. With `live`, also only while the fire
    the credential was minted in still runs (`routine_execution`): a finished, timed-out
    or cancelled run, or a credential that names no fire, names nobody.
    """
    if binding.routine_id is None or (live and binding.execution is None):
        return None
    async with sessionmaker() as session:
        row = await get_routine(session, binding.routine_id, tenant_id=tenant_id)
        if (
            row is None
            or not row.created_by_user_id
            or row.created_by_user_id != binding.owner
            or derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=row.agent_id) != agent_id
        ):
            return None
        if live:
            assert binding.execution is not None
            if not await execution_is_active(
                session, tenant_id, binding.routine_id, binding.execution
            ):
                return None
    return row


async def _workflow_step_run(
    sessionmaker: async_sessionmaker[AsyncSession], binding: RelayBinding, *, tenant_id: uuid.UUID
) -> BoundStep | None:
    """The agent step a lane's credential serves now (`bound_step`), by `_routine_run`'s rule:
    only while its run-as person is still the one the session was minted for."""
    if binding.workflow_run_id is None or binding.owner is None:
        return None
    async with sessionmaker() as session:
        step = await bound_step(
            session,
            tenant_id=tenant_id,
            run_id=binding.workflow_run_id,
            execution_id=binding.execution,
            agent_name=binding.agent_name,
        )
    return step if step is not None and step.subject.created_by_user_id == binding.owner else None


def _workflow_claims(
    step: BoundStep, platform: str, workspace: str | None
) -> dict[str, str | None]:
    """An agent step's workflow claims: its id, name and, while it is active, the revision a
    server's approval is bound to (`routine_revision` of the step's facts)."""
    signed = f"{platform}:{workspace}" if workspace else None
    return {
        "workflow": str(step.workflow.id),
        "workflow_rev": routine_revision(step.subject, workspace=signed)
        if step.workflow.status == "active"
        else None,
        "workflow_name": step.workflow.name,
    }


async def _dm_routine_run(
    sessionmaker: async_sessionmaker[AsyncSession],
    binding: RelayBinding,
    origin: TurnOriginRow,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    person: str,
    now: datetime,
) -> RoutineRow | None:
    """The routine whose live fire posting to its owner's DM a call comes from, or None.

    `origin` is the turn holding the credential's own grant. It must be that routine's
    unattended turn in a fresh `dm:` scope: no person's message, not a helper's, not a
    watch's or timer's (they never reach the owner). The owner is the signed-in person,
    the credential's and the routine's, which still posts to that person's DM, runs this
    agent, is enabled and unedited since the fire began; and the fire is still running
    (`routine_execution`). Checked on every call, so a pause or an edit stops the next.
    """
    if (
        binding.execution is None
        or binding.revision is None
        or binding.dm_channel is None
        or binding.routine_id is None
        or binding.destination != DM_ROUTINE_DESTINATION
        or binding.owner != person
        or origin.helper_of is not None
        or origin.human_invocation is not False
        or origin.attended
        or not origin.own_dm_reach
        or origin.platform != "discord"
        or not origin.thread_id.startswith(DM_SCOPE_PREFIX)
        or origin.parent_channel_id != origin.thread_id
        or origin.platform_channel_id is not None
        or origin.platform_message_id is not None
        or origin.expires_at <= now
    ):
        return None
    async with sessionmaker() as session:
        row = await get_routine(session, binding.routine_id, tenant_id=tenant_id)
        if (
            row is None
            or not row.enabled
            or row.destination_kind != "dm"
            or not row.created_by_user_id == row.destination_id == person
            or routine_destination_key(row) != DM_ROUTINE_DESTINATION
            or derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=row.agent_id) != agent_id
            or row.updated_at != datetime.fromisoformat(binding.revision)
            or not await execution_is_active(
                session, tenant_id, binding.routine_id, binding.execution
            )
        ):
            return None
        return row


async def _watch_poll(
    sessionmaker: async_sessionmaker[AsyncSession],
    binding: RelayBinding,
    *,
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
    agent_id: uuid.UUID,
    server: SignedIdentityServer,
    public_url: str | None,
) -> WatchRow | None:
    """The active watch a poll's credential names, or None.

    Only a watch of this tenant and agent, still owned by the account and user the
    credential was minted for and still polling this relay server, counts; a paused,
    deleted or handed-over watch names nobody.
    """
    if binding.watch_id is None or public_url is None:
        return None
    async with sessionmaker() as session:
        row = await watches.get_watch(session, binding.watch_id)
    if (
        row is None
        or row.status != "active"
        or row.source != "mcp_poll"
        or (row.tenant_id, row.owner_account_id) != (tenant_id, account_id)
        or row.owner_external_id != binding.owner
        or derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=row.agent_id) != agent_id
        or row.source_state.get("credential") != "relay"
        or not same_ascii_mcp_url(
            str(row.source_state.get("server_url")), relay_url(public_url, server.name)
        )
    ):
        return None
    return row


def _is_poll_message(message: object, tool: str) -> bool:
    """Whether a relayed body is part of a watch poll: the handshake, a tool listing or
    one call of the watch's own tool. A watch's credential is for nothing else."""
    if not isinstance(message, dict):
        return False
    fields = cast("dict[str, object]", message)
    method, params = fields.get("method"), fields.get("params")
    if method in ("initialize", "notifications/initialized", "tools/list"):
        return True
    return (
        method == "tools/call"
        and isinstance(params, dict)
        and cast("dict[str, object]", params).get("name") == tool
    )


async def _listed_as_builtin(
    anthropic: AsyncAnthropic | None,
    sessionmaker: async_sessionmaker[AsyncSession],
    server: SignedIdentityServer,
    binding: RelayBinding,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
) -> bool:
    """Whether the seeded agent signs one built-in name and the allowlist the other.

    Its rename changes the name a credential carries; a workspace's own agent
    of either name never passes this way.
    """
    if normalize_agent_name(binding.agent_name) not in BUILTIN_AGENT_NAMES or not any(
        normalize_agent_name(name) in BUILTIN_AGENT_NAMES for name in server.agents
    ):
        return False
    try:
        agent = await find_agent_or_builtin(
            anthropic,
            tenant_id=tenant_id,
            name=binding.agent_name,
            records=sessionmaker,
            surface="identity_relay",
        )
    except Exception as exc:  # boundary: an unverified agent is refused
        log.warning("mcp_identity.relay_builtin_lookup_failed", error_type=type(exc).__name__)
        return False
    return (
        agent is not None
        and agent.metadata.get(MA_METADATA_KEY_MANAGED) == "true"
        and derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=agent.id) == agent_id
    )


async def _held_in_audience_mode_on(
    sessionmaker: async_sessionmaker[AsyncSession],
    raw_hold: object,
    *,
    tenant_id: uuid.UUID,
    platform: str,
) -> bool:
    """Whether a held token is one of a room outsiders may read, or nobody can vouch for, in
    mode `on`. Such a session mounts no signed-identity server there, so the token is a
    leftover. A relay-only hold of an internal room (`relay=True`) keeps working: the room's
    facts are read again here. An unreadable hold, mode or fact counts as yes: fail closed."""
    try:
        hold = parse_hold_claim(raw_hold)
        async with sessionmaker() as session:
            mode = await read_audience_mode(
                sessionmaker,
                tenant_id=tenant_id,
                platform=hold.platform,
                channel_id=hold.channel_id,
                session=session,
            )
            if mode != "on":
                return False
            audience = await channel_has_outsiders(
                session, tenant_id=tenant_id, platform=hold.platform, channel_id=hold.channel_id
            )
    except Exception:
        log.warning("mcp_identity.relay_hold_unreadable", exc_info=True)
        return True
    return audience.contained


def _refused(
    status: int,
    reason: str,
    *,
    server: SignedIdentityServer | None = None,
    tenant_id: uuid.UUID | None = None,
    sessions: async_sessionmaker[AsyncSession] | None = None,
) -> Response:
    """A refusal that used to be silent, with a fixed reason. The tenant is named only once
    the credential resolved it, and no server name is logged for one the relay does not have.
    With `sessions`, a refusal of a resolved tenant is also recorded as an issue."""
    # Scanner noise (an unknown path, a missing bearer) is info; the rest is a warning.
    noise = reason in {"unknown_server", "no_bearer"} and tenant_id is None
    (log.info if noise else log.warning)(
        "mcp_identity.relay_refused",
        status=status,
        reason=reason,
        server=server.name if server is not None else None,
        tenant_id=str(tenant_id) if tenant_id is not None else None,
    )
    if sessions is not None:
        _report_relay(sessions, "bridge.relay_refused", server, tenant_id, str(status))
    return Response(status_code=status)


def _report_relay(
    sessions: async_sessionmaker[AsyncSession],
    code: OpsCode,
    server: SignedIdentityServer | None,
    tenant_id: uuid.UUID | None,
    reason: str,
) -> None:
    subject = server.name if server is not None else None
    report_ops_event(
        sessions, OpsEvent(code, tenant_id, reason=reason, subject_key=subject), logged=False
    )


def _claimed_tenant(claims: Mapping[str, object]) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(claims["tenant_id"]))
    except (KeyError, ValueError):
        return None


async def _bearer_tenant(auth: TokenVerifier, request: Request) -> uuid.UUID | None:
    """The tenant a valid bearer names, so a refusal before its server is known is reported."""
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("bearer "):
        return None
    access = await auth.verify_token(header[len("bearer ") :])
    return _claimed_tenant(access.claims) if access is not None else None


def _tool_name(request: Request, message: object, body: bytes) -> str | None:
    """The tool a relayed `tools/call` names, clipped; never its arguments."""
    if message is None and request.method == "POST":
        try:
            message = read_request_body(body)
        except RequesterBodyError:
            return None
    name = called_tool(message)
    return name[:64] if name is not None else None


def build_identity_relay_route(
    *,
    servers: Mapping[str, SignedIdentityServer],
    auth: TokenVerifier,
    sessionmaker: async_sessionmaker[AsyncSession],
    tokens: IdentitySigner | None,
    clients: Mapping[str, httpx.AsyncClient],
    clock: Callable[[], datetime],
    bearers: Mapping[str, SecretStr] | None = None,
    anthropic: AsyncAnthropic | None = None,
    public_url: str | None = None,
) -> Callable[[Request], Awaitable[Response]]:
    """The relay route. `tokens` signs for servers whose auth is signed; `bearers` holds
    the key of each server whose auth is bearer. A server with neither is refused (503)."""
    keys = bearers or {}

    async def handler(request: Request) -> Response:
        server = servers.get(request.path_params["name"])
        if server is None:
            tenant = await _bearer_tenant(auth, request)
            return _refused(404, "unknown_server", tenant_id=tenant, sessions=sessionmaker)
        header = request.headers.get("authorization", "")
        if not header.lower().startswith("bearer "):
            return _refused(401, "no_bearer", server=server)
        access = await auth.verify_token(header[len("bearer ") :])
        if access is None:
            return _refused(401, "token_rejected", server=server)
        claims = access.claims
        # Only a session's relay credential: it names the executing agent and the
        # session. Daimon's own chat credential, coding, operator and CLI keys do not.
        try:
            account_id = uuid.UUID(str(claims["sub"]))
            tenant_id = uuid.UUID(str(claims["tenant_id"]))
            agent_id = uuid.UUID(str(claims["chat_agent_id"]))
            binding = parse_relay_claim(claims[RELAY_CLAIM])
        except (KeyError, ValueError):
            return _refused(
                403,
                "not_session_credential",
                server=server,
                tenant_id=_claimed_tenant(claims),
                sessions=sessionmaker,
            )
        if claims.get(TOKEN_KIND_CLAIM) != "agent":
            return _refused(
                403, "not_agent_token", server=server, tenant_id=tenant_id, sessions=sessionmaker
            )
        platform = str(claims.get("platform") or "")
        if HOLD_CLAIM in claims and await _held_in_audience_mode_on(
            sessionmaker, claims[HOLD_CLAIM], tenant_id=tenant_id, platform=platform
        ):
            # A held session is in a room outsiders may read, or nobody can vouch for: in
            # `on` it mounts no signed-identity server, so this token is a leftover. Refused.
            return _refused(
                403, "held_session", server=server, tenant_id=tenant_id, sessions=sessionmaker
            )
        workspace = claims.get("external_id")
        workspace = workspace if isinstance(workspace, str) else None
        if server.workspaces is not None and f"{platform}:{workspace}" not in server.workspaces:
            return _refused(
                403,
                "workspace_not_allowed",
                server=server,
                tenant_id=tenant_id,
                sessions=sessionmaker,
            )
        # An allowlist names the agents that may use a server (a bearer server always has one).
        if (
            server.agents
            and binding.agent_name not in server.agents
            and not await _listed_as_builtin(
                anthropic, sessionmaker, server, binding, tenant_id=tenant_id, agent_id=agent_id
            )
        ):
            return _refused(
                403, "agent_not_allowed", server=server, tenant_id=tenant_id, sessions=sessionmaker
            )
        bearer = keys.get(server.name)
        if (server.auth == "bearer" and bearer is None) or (
            server.auth == "signed" and tokens is None
        ):
            log.error(
                "mcp_identity.relay_unconfigured", server=server.name, tenant_id=str(tenant_id)
            )
            _report_relay(
                sessionmaker, "bridge.relay_failed", server, tenant_id, "relay_unconfigured"
            )
            return Response(status_code=503)
        person = claims.get("platform_user_id")
        body = await read_capped_body(request)
        if body is None:
            return _refused(
                413, "body_too_large", server=server, tenant_id=tenant_id, sessions=sessionmaker
            )
        now = clock()
        origin, granted = await _session_turn(
            sessionmaker,
            binding,
            tenant_id=tenant_id,
            account_id=account_id,
            agent_id=agent_id,
            platform=platform,
            now=now,
        )
        # A sender from another organisation is not one of the workspace's own
        # people: the server must not apply a member's permissions to them.
        if origin is not None and (origin.sender_foreign or FOREIGN_HOME_CLAIM in claims):
            origin = None
        watch: WatchRow | None = None
        if binding.watch_id is not None:
            # A poll's credential is for its watch alone; a gone or handed-over watch
            # names no tool to allow, so it reaches nothing.
            watch = await _watch_poll(
                sessionmaker,
                binding,
                tenant_id=tenant_id,
                account_id=account_id,
                agent_id=agent_id,
                server=server,
                public_url=public_url,
            )
            if watch is None or request.method != "POST":
                return _refused(
                    403,
                    "watch_poll_refused",
                    server=server,
                    tenant_id=tenant_id,
                    sessions=sessionmaker,
                )
        message: object = None
        if watch is not None or (server.requester_meta and request.method == "POST"):
            try:
                message = read_request_body(body)
            except RequesterBodyError:
                return _refused(
                    400, "bad_body", server=server, tenant_id=tenant_id, sessions=sessionmaker
                )
        if watch is not None and not _is_poll_message(message, str(watch.source_state.get("tool"))):
            return _refused(
                403,
                "watch_poll_tool_refused",
                server=server,
                tenant_id=tenant_id,
                sessions=sessionmaker,
            )
        headers = {
            name: value
            for name in _REQUEST_HEADERS
            if (value := request.headers.get(name)) is not None
        }
        if server.auth == "bearer":
            assert bearer is not None
            headers["authorization"] = f"Bearer {bearer.get_secret_value()}"
        else:
            assert tokens is not None
            place = origin_turn_place(origin) if origin is not None else None
            room = (
                await turn_room(sessionmaker, origin, now=now)
                if origin is not None and origin.platform_channel_id
                else None
            )
            routine_id: str | None = None
            signed_routine: RoutineRow | None = None
            signed_step: BoundStep | None = None
            if (
                server.routine_claims
                and origin is None
                and binding.routine_id is not None
                and binding.session_key is None
                and FOREIGN_HOME_CLAIM not in claims
            ):
                # Only a run the routine check passes, in a fire still running, still
                # posting where it did when the run started, and only for that room: the
                # channel comes from the routine's own row, never from the request. A
                # credential minted before runs kept their destination or their fire
                # names neither, so it signs no routine claim.
                routine = await _routine_run(
                    sessionmaker, binding, tenant_id=tenant_id, agent_id=agent_id, live=True
                )
                if routine is not None and routine_destination_key(routine) != binding.destination:
                    routine = None
                destination = (
                    await routine_room(
                        sessionmaker,
                        tenant_id=tenant_id,
                        platform=platform,
                        destination=routine_conversation(routine),
                        now=now,
                    )
                    if routine is not None
                    else None
                )
                if destination is not None:
                    routine_id, signed_routine = str(binding.routine_id), routine
                    place, room = (
                        TurnPlace(channel_id=destination[0], message_id=None),
                        destination[1],
                    )
            elif (
                server.routine_claims
                and server.dm_routine_claims
                and origin is not None
                and granted
                and binding.session_key is not None
                and binding.dm_channel is not None
                and isinstance(person, str)
                and person
                and (
                    dm_routine := await _dm_routine_run(
                        sessionmaker,
                        binding,
                        origin,
                        tenant_id=tenant_id,
                        agent_id=agent_id,
                        person=person,
                        now=now,
                    )
                )
                is not None
            ):
                # A routine posting to its owner's DM, in a live fire: the owner, their DM
                # (opened by the fire, never the origin's) and room `dm`. Any check failing
                # leaves the call as it is without the setting.
                routine_id, signed_routine = str(binding.routine_id), dm_routine
                place, room = TurnPlace(channel_id=binding.dm_channel, message_id=None), "dm"
            elif (
                server.routine_claims
                and origin is None
                and binding.workflow_run_id is not None
                and FOREIGN_HOME_CLAIM not in claims
                and (step := await _workflow_step_run(sessionmaker, binding, tenant_id=tenant_id))
                is not None
            ):
                # An agent step signs its workflow for the room its `at` names now, read from
                # the run, or for a private step its run-as person's DM the lane opened.
                destination = await routine_room(
                    sessionmaker,
                    tenant_id=tenant_id,
                    platform=platform,
                    destination=routine_conversation(step.subject),
                    now=now,
                )
                if destination is not None:
                    signed_step = step
                    place, room = (
                        TurnPlace(channel_id=destination[0], message_id=None),
                        destination[1],
                    )
                elif (
                    step.at is None
                    and server.dm_routine_claims
                    and binding.dm_channel is not None
                    and person == binding.owner
                ):
                    signed_step = step
                    place, room = TurnPlace(channel_id=binding.dm_channel, message_id=None), "dm"
            wake: WakeReason | None = None
            if (
                server.wake_claims
                and routine_id is None
                and signed_step is None
                and origin is not None
                and granted
                and isinstance(person, str)
                and person
            ):
                # Only the turn holding this credential's own grant. A person's live turn
                # keeps everything it signs; a continuation signs its thread as a routine
                # posting there would; a watch report to its owner's DM signs that DM as a
                # DM routine does. Anything else signs what it signs without the setting.
                if _live_person_turn(origin, now=now):
                    wake = "turn"
                elif (
                    server.dm_routine_claims
                    and binding.dm_channel is not None
                    and await _dm_wake(
                        sessionmaker, binding, origin, tenant_id=tenant_id,
                        account_id=account_id, person=person, platform=platform, now=now,
                    )
                ):  # fmt: skip
                    wake, room = "watch_report", "dm"
                    place = TurnPlace(channel_id=binding.dm_channel, message_id=None)
                elif (
                    running := await _thread_wake(
                        sessionmaker,
                        origin,
                        tenant_id=tenant_id,
                        account_id=account_id,
                        person=person,
                        platform=platform,
                        now=now,
                    )
                ) is not None:
                    wake, channel, room = running
                    place = TurnPlace(channel_id=channel, message_id=None)
            identity = caller_claims(
                issuer=server.issuer,
                audience=server.audience,
                agent_id=str(agent_id),
                platform=platform,
                workspace_id=workspace,
                platform_user_id=person if isinstance(person, str) else None,
                agent_name=origin.responder_name if origin is not None else binding.agent_name,
                place=place,
                room=room,
                routine=routine_id,
                wake=wake,
                # What a server's approval of this routine is bound to, from the row just
                # checked; none for a routine that is paused, so no approval counts there.
                rev=(
                    routine_revision(
                        signed_routine, workspace=f"{platform}:{workspace}" if workspace else None
                    )
                    if signed_routine is not None and signed_routine.enabled
                    else None
                ),
                routine_name=signed_routine.name if signed_routine is not None else None,
                **(_workflow_claims(signed_step, platform, workspace) if signed_step else {}),
                # A channel or thread routine's owner (a step's run-as person), for what
                # the run makes for them.
                routine_owner=owned.created_by_user_id
                if (owned := signed_routine or (signed_step.subject if signed_step else None))
                and room != "dm"
                else None,
            )
            headers["authorization"] = f"Bearer {tokens.token(identity, now=now)}"
        if request.method in ("GET", "DELETE"):
            # Neither carries a message; a body on one is dropped, not forwarded.
            body = b""
        if server.requester_meta and request.method == "POST":
            # A turn a person started names that person. An unattended run names who
            # it acts for, as kind "routine": a routine's owner and delivery place, a
            # watch poll's owner and report place, or the person and place of a wake
            # (watch, timer, continuation). An agent-chat session names the person
            # signed in to the hub who drives it, as kind "agent_chat" with no place, on a
            # server that opted in. A foreign sender, or a session whose calling turn is
            # unknown (`_session_turn`), matches none and carries no requester fields.
            named: str | None = None
            conversation: dict[str, str | None] | None = None
            kind: RequesterKind | None = None
            if (
                origin is not None
                and platform in REQUESTER_PLATFORMS
                and isinstance(person, str)
                and person
            ):
                kind = "person" if origin.human_invocation is True else "routine"
                named = person
                conversation = requester_conversation(origin, unattended=kind == "routine")
            elif (
                binding.routine_id is not None
                and binding.session_key is None
                and platform in REQUESTER_PLATFORMS
                and FOREIGN_HOME_CLAIM not in claims
            ):
                routine = await _routine_run(
                    sessionmaker, binding, tenant_id=tenant_id, agent_id=agent_id
                )
                if routine is not None:
                    named, conversation, kind = (
                        routine.created_by_user_id,
                        routine_conversation(routine),
                        "routine",
                    )
            elif (
                binding.workflow_run_id is not None
                and platform in REQUESTER_PLATFORMS
                and FOREIGN_HOME_CLAIM not in claims
                and (step := await _workflow_step_run(sessionmaker, binding, tenant_id=tenant_id))
                is not None
            ):
                named, conversation, kind = (
                    step.subject.created_by_user_id,
                    routine_conversation(step.subject)
                    or (dict(UNOPENED_DM_PLACE) if binding.dm_channel else None),
                    "routine",
                )
            elif (
                watch is not None
                and platform in REQUESTER_PLATFORMS
                and FOREIGN_HOME_CLAIM not in claims
            ):
                named, conversation, kind = (
                    watch.owner_external_id,
                    watch_conversation(watch),
                    "routine",
                )
            elif (
                binding.agent_chat is not None
                and server.agent_chat_requester
                and platform in REQUESTER_PLATFORMS
                and FOREIGN_HOME_CLAIM not in claims
                and isinstance(person, str)
                and person
                and await driver_signed_in(sessionmaker, managed_session_id=binding.agent_chat)
            ):
                # The credential's own account's platform user, read live by the
                # verifier; the request body cannot set it (`stamp_parsed`).
                named, kind = person, "agent_chat"
            wake_url: str | None = None
            # Only a person's call that names them and their place gets a link.
            if (
                kind == "person"
                and origin is not None
                and named is not None
                and conversation
                and called_tool(message) in server.wake_tools
            ):
                wake_url = await _wake_link(
                    sessionmaker,
                    origin,
                    tenant_id=tenant_id,
                    account_id=account_id,
                    person=named,
                    server=server.name,
                    now=now,
                )
            try:
                body = stamp_parsed(
                    message, person=named, conversation=conversation, kind=kind, wake_url=wake_url
                )
            except RequesterBodyError:
                return _refused(
                    400, "bad_body", server=server, tenant_id=tenant_id, sessions=sessionmaker
                )
        http = clients[server.name]
        upstream = http.build_request(
            request.method,
            server.url,
            headers=headers,
            content=body,
            # A GET is the server's standing SSE stream, quiet between events.
            timeout=(
                httpx.Timeout(30.0, read=None, pool=POOL_TIMEOUT)
                if request.method == "GET"
                else httpx.USE_CLIENT_DEFAULT
            ),
        )
        try:
            response = await http.send(upstream, stream=True)
        except httpx.PoolTimeout:
            log.warning(
                "mcp_identity.relay_busy",
                server=server.name,
                tenant_id=str(tenant_id),
                tool=_tool_name(request, message, body),
            )
            _report_relay(sessionmaker, "bridge.relay_failed", server, tenant_id, "relay_busy")
            return Response(status_code=503, headers={"retry-after": "5"})
        except httpx.HTTPError as err:
            log.warning(
                "mcp_identity.relay_failed",
                server=server.name,
                tenant_id=str(tenant_id),
                tool=_tool_name(request, message, body),
                error_type=type(err).__name__,
            )
            _report_relay(sessionmaker, "bridge.relay_failed", server, tenant_id, "502")
            return Response(status_code=502)
        # An upstream 5xx is a failed relay for alerting; the call still reaches the caller.
        # The tool name is read for failure lines only, never on the success path.
        if response.status_code >= 500:
            log.warning(
                "mcp_identity.relay_failed",
                server=server.name,
                tenant_id=str(tenant_id),
                tool=_tool_name(request, message, body),
                status=response.status_code,
                person=origin is not None,
                auth=server.auth,
            )
            _report_relay(
                sessionmaker, "bridge.relay_failed", server, tenant_id, str(response.status_code)
            )
        else:
            log.info(
                "mcp_identity.request_relayed",
                server=server.name,
                tenant_id=str(tenant_id),
                status=response.status_code,
                person=origin is not None,
                auth=server.auth,
            )
        return StreamingResponse(
            response.aiter_bytes(),
            status_code=response.status_code,
            headers={
                name: value
                for name in _RESPONSE_HEADERS
                if (value := response.headers.get(name)) is not None
            },
            background=BackgroundTask(response.aclose),
        )

    return handler
