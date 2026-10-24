"""The one gate for turns nobody is watching.

Two halves, both from the current policy. Before every unattended send,
`gate_session_snapshot` rewrites the session's own tool config so every gated
call pauses: a frozen `always_allow` would otherwise skip the decision
entirely. Every pause is then decided by `unattended_posture`, which always
answers through `unattended_decider` and never `AutoApprove`. Safety off is
the explicit `enabled=False` policy from settings, under which the decider
allows ordinary third-party calls. Prepared fleet/wake turns require a fresh
Managed Agents snapshot before every confirmation batch; a Managed Agents
runtime without that read is refused before its first message
(`run_prepared_turn`). A non-Anthropic runtime refuses every unattended
confirmation, even with safety off; it never falls back to an allow decision.

What the session may still do beyond gated MCP tools (a coordinator, the
built-in sandbox, skills, fields this release does not know) is decided per
entry point in `unattended_capabilities`, the one place for it.

Daimon's own protected actions (`protected_tools`) ask in these sessions and
their final call is refused, since nobody is there to press the card: a
backend switch and Microsoft sends with safety on or off, Microsoft writes
with it on, the same selection a chat turn asks on.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import replace
from typing import TYPE_CHECKING

from anthropic import AsyncAnthropic
from anthropic.types.beta import BetaManagedAgentsSession
from cryptography.fernet import MultiFernet
from daimon.core.defaults.metadata import (
    MA_METADATA_KEY_ACCOUNT,
    MA_METADATA_KEY_CHANNEL,
    MA_METADATA_KEY_PRIVATE_DM,
    MA_METADATA_KEY_TENANT,
)
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.mcp_tool_annotations import (
    agent_mcp_token_for,
    mcp_annotation_lookup,
    no_mcp_token,
)
from daimon.core.mux_backend import resource_scope
from daimon.core.session_ports_compat import retrieve_session_record
from daimon.core.tool_safety import (
    ToolCall,
    ToolSafetyPolicy,
    daimon_endpoint,
    trusted_servers_for,
)
from daimon.core.turn.approvals import (
    AnnotationLookup,
    agent_rule_lookup,
    channel_for_tool_rules,
    unattended_decider,
)
from daimon.core.turn.errors import AdmissionDenied
from daimon.core.turn.posture import PolicyApproval, ToolConfirmationResult
from daimon.core.turn.unattended_capabilities import (
    ServerTool,
    UnattendedEntryPoint,
    apply_capabilities,
    capability_policy,
    capability_posture,
    constrain_capabilities,
    protected_tools,
)
from mux.contracts.ids import Scope
from mux.contracts.resources import AgentSpec
from mux.profiles import get_profile
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

if TYPE_CHECKING:
    from daimon.core.turn.admission import Admission
    from daimon.core.turn.deps import TurnDeps


def require_native_unattended_capabilities(spec: AgentSpec | None, *, profile_id: str) -> None:
    """Refuse unreviewed or automatic native sandbox/custom tools before send.

    Selfhosted inspects every call at its host boundary and Managed Agents
    rewrites its session. Other providers must expose a reviewed native
    target; their automatic tools cannot be stopped by refusing a pause.
    """
    if get_profile(profile_id).implicit_execution:
        # Hosted OpenAI bash is implicit in its environment, so an empty
        # agent.tools readback does not prove that execution is disabled.
        raise AdmissionDenied(
            reason="backend_unsupported",
            detail="Automatic sandbox or custom tools cannot run without someone watching.",
        )
    if spec is None:
        raise AdmissionDenied(
            reason="backend_unsupported",
            detail="The tool configuration cannot be checked for a reply nobody is watching.",
        )
    if any(
        tool.kind in {"builtin", "custom"} and tool.permission == "auto"
        for tool in spec.tools or ()
    ):
        raise AdmissionDenied(
            reason="backend_unsupported",
            detail="Automatic sandbox or custom tools cannot run without someone watching.",
        )
    if (
        any(tool.permission == "auto" for tool in spec.tools or ())
        or spec.mcp_servers
        or spec.extensions
        or spec.model.options
    ):
        # These runtimes have no host inspection of automatic MCP calls or
        # provider-specific capabilities. Refusing their confirmation pauses
        # would not prevent an automatic call from executing.
        raise AdmissionDenied(
            reason="backend_unsupported",
            detail=(
                "This model provider cannot inspect these tools before running "
                "without someone watching."
            ),
        )


async def refuse_ungated_confirmation(call: ToolCall) -> ToolConfirmationResult:
    """Refuse a prepared unattended call whose runtime cannot refresh its gate."""
    return ToolConfirmationResult(
        allow=False,
        deny_message=(
            f"Refused {call.key}: this model provider cannot refresh the conversation's "
            "tool safety "
            "gate before unattended confirmations. The tool call did not run."
        ),
    )


async def gate_session_snapshot(
    client: AsyncAnthropic,
    session: BetaManagedAgentsSession,
    policy: ToolSafetyPolicy,
    *,
    scope: Scope,
    public_url: str | None,
    session_factory: async_sessionmaker[AsyncSession] | None,
    tenant_id: uuid.UUID | None,
    entry_point: UnattendedEntryPoint | None = None,
    routine_sandbox_tools: bool = False,
    tool_allowlist: frozenset[ServerTool] | None = None,
) -> None:
    """Make `session`'s gated calls pause under `policy`, before anything is sent.

    Works from the session's own frozen arrays, so hidden servers and the
    caller's resources stay as they are; nothing is restored from the
    agent's deployment-wide definition. `constrain_capabilities` refuses a
    session it cannot constrain (a coordinator, skills the `entry_point`
    does not allow, an unknown field) before any update, turns built-ins off
    unless `routine_sandbox_tools` opts a `routine` in (`capability_policy`),
    rewrites
    both the toolset defaults and every per-tool config, makes Daimon's
    `protected_tools` ask, and heals a foreign `daimon-mcp` URL, policy on or
    off. A refusal or a failed update raises, so the send never happens on an
    ungated session. An `entry_point` of None gets no built-ins and no skills.
    """
    allowed = capability_policy(entry_point, routine_sandbox_tools=routine_sandbox_tools)
    constrained = await constrain_capabilities(
        session,
        policy,
        public_url=public_url,
        session_factory=session_factory,
        tenant_id=tenant_id,
        allow_builtins=allowed.allow_builtins,
        allow_skills=allowed.allow_skills,
        tool_allowlist=frozenset() if entry_point == "long_job" else tool_allowlist,
    )
    await apply_capabilities(
        client, session, constrained, scope=scope, session_factory=session_factory
    )


def session_annotations(
    policy: ToolSafetyPolicy,
    session: BetaManagedAgentsSession,
    *,
    session_factory: async_sessionmaker[AsyncSession] | None,
    tenant_id: uuid.UUID | None,
    fernet: MultiFernet | None = None,
    agent_id: uuid.UUID | None = None,
) -> AnnotationLookup | None:
    """Bind trusted catalogues and credentials to the session being executed."""
    if not policy.enabled or not policy.trusted_annotations:
        return None
    return mcp_annotation_lookup(
        server_urls={
            s.name: s.url for s in session.agent.mcp_servers if s.name in policy.trusted_annotations
        },
        token_for=agent_mcp_token_for(
            session_factory,
            fernet=fernet,
            tenant_id=tenant_id,
            agent_id=agent_id
            or derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=session.agent.id),
        )
        if session_factory is not None and tenant_id is not None
        else no_mcp_token,
    )


def unattended_posture(
    policy: ToolSafetyPolicy,
    *,
    session: BetaManagedAgentsSession,
    public_url: str | None,
    session_factory: async_sessionmaker[AsyncSession] | None,
    tenant_id: uuid.UUID | None,
    channel_id: str | None = None,
    annotations_for: AnnotationLookup | None = None,
    fernet: MultiFernet | None = None,
    agent_id: uuid.UUID | None = None,
    entry_point: UnattendedEntryPoint | None = None,
    routine_sandbox_tools: bool = False,
    tool_allowlist: frozenset[ServerTool] | None = None,
) -> PolicyApproval:
    """How an unattended turn on `session` answers its pauses.

    Always `unattended_decider` over `policy`, with the agent's rules bound to
    the servers this session really talks to and to `channel_id`, the
    channel whose rules the turn runs under (`channel_for_tool_rules`; None
    for a DM or no channel); with no database or tenant there are no rules to
    read. `protected_tools` are refused. `annotations_for`
    is the seam for operator-trusted MCP hints; it must answer only for
    trusted servers. A built-in or custom call is refused (`capability_posture`)
    unless `routine_sandbox_tools` opts a `routine` in; no other `entry_point`
    can be opted in.
    """
    posture = PolicyApproval(
        decide=unattended_decider(
            policy,
            trusted_servers=trusted_servers_for(
                public_url, {s.name: s.url for s in session.agent.mcp_servers}
            ),
            confirmed_tools=protected_tools(policy),
            rules_for=agent_rule_lookup(
                session_factory,
                tenant_id=tenant_id,
                agent_id=session.agent.id,
                server_urls={s.name: s.url for s in session.agent.mcp_servers},
                also_urls=daimon_endpoint(public_url),
                channel_id=channel_id,
            )
            if session_factory is not None and tenant_id is not None
            else None,
            daimon_rules=True,
            annotations_for=annotations_for
            or session_annotations(
                policy,
                session,
                session_factory=session_factory,
                tenant_id=tenant_id,
                fernet=fernet,
                agent_id=agent_id,
            ),
        )
    )
    return capability_posture(
        posture,
        allow_builtins=capability_policy(
            entry_point, routine_sandbox_tools=routine_sandbox_tools
        ).allow_builtins,
        tool_allowlist=frozenset() if entry_point == "long_job" else tool_allowlist,
    )


def session_gate(
    client: AsyncAnthropic,
    session_id: str,
    policy: ToolSafetyPolicy,
    *,
    scope: Scope,
    public_url: str | None,
    session_factory: async_sessionmaker[AsyncSession] | None,
    tenant_id: uuid.UUID | None,
    read_session: Callable[[], Awaitable[BetaManagedAgentsSession]] | None = None,
    entry_point: UnattendedEntryPoint | None = None,
    routine_sandbox_tools: bool = False,
    tool_allowlist: frozenset[ServerTool] | None = None,
) -> Callable[[], Awaitable[None]]:
    """The gate a driven unattended turn runs before each send (`unattended_sends`).

    It reads the session as it is now and gates that snapshot, so a policy or
    rule that changed while the turn was paused reaches its next call. A
    failed read or update raises, and the send it guards does not happen.
    An injected mux backend supplies its own `read_session`; its snapshot is
    never replaced by a read through a different runtime. `entry_point`
    and `routine_sandbox_tools` pick the `CapabilityPolicy`
    (`gate_session_snapshot`).
    """

    async def gate() -> None:
        await gate_session_snapshot(
            client.with_options(max_retries=0),
            await read_session()
            if read_session is not None
            else await retrieve_session_record(client, session_id, scope=scope),
            policy,
            scope=scope,
            public_url=public_url,
            session_factory=session_factory,
            tenant_id=tenant_id,
            entry_point=entry_point,
            routine_sandbox_tools=routine_sandbox_tools,
            tool_allowlist=tool_allowlist,
        )

    return gate


def session_rules_channel(session: BetaManagedAgentsSession, *, platform: str) -> str | None:
    """The channel whose tool rules a turn on `session` runs under.

    The conversation's own stamped channel, a Slack thread's parent, and never
    a DM's source channel (`channel_for_tool_rules`); not the channel its spend
    is billed to.
    """
    metadata = session.metadata or {}
    return channel_for_tool_rules(
        platform=platform,
        channel_id=metadata.get(MA_METADATA_KEY_CHANNEL),
        private_dm_id=metadata.get(MA_METADATA_KEY_PRIVATE_DM),
    )


async def dm_session_authority(
    deps: TurnDeps,
    admission: Admission,
    session_id: str,
    *,
    tenant_id: uuid.UUID,
    read_session: Callable[[], Awaitable[BetaManagedAgentsSession]] | None = None,
    entry_point: UnattendedEntryPoint | None = None,
) -> tuple[PolicyApproval, Callable[[], Awaitable[None]]]:
    """Authorize unattended DMs entirely from their actual session snapshot.

    A replacement gets a new authority; an in-place identity or endpoint change
    refuses the batch instead of combining rules and hints from different agents.
    """
    from daimon.core.turn.admission import decide_before_send

    scope = resource_scope(
        tenant_id=str(tenant_id),
        account_id=str(admission.account_id),
        authorization_id="dm-unattended",
    )

    async def read() -> BetaManagedAgentsSession:
        return (
            await read_session()
            if read_session is not None
            else await retrieve_session_record(deps.anthropic, session_id, scope=scope)
        )

    actual = await read()
    metadata = actual.metadata or {}
    if (
        actual.id != session_id
        or actual.agent.id != admission.agent.id
        or metadata.get(MA_METADATA_KEY_TENANT) != str(tenant_id)
        or metadata.get(MA_METADATA_KEY_ACCOUNT) != str(admission.account_id)
    ):
        raise AdmissionDenied(reason="invoker_not_allowed")
    if admission.grant is not None:
        grant = admission.grant
        admission = replace(
            admission,
            grant=replace(
                grant, agent=replace(grant.agent, names=(*grant.agent.names, actual.agent.name))
            ),
        )
    urls = {server.name: server.url for server in actual.agent.mcp_servers}
    posture = unattended_posture(
        deps.tool_safety,
        session=actual,
        public_url=deps.public_url,
        session_factory=deps.sessionmaker,
        tenant_id=tenant_id,
        channel_id=None,
        fernet=deps.fernet,
        entry_point=entry_point,
    )

    async def gate() -> None:
        await decide_before_send(deps, admission, attended=False)()
        current = await read()
        if (
            current.id != actual.id
            or current.agent.id != actual.agent.id
            or current.agent.name != actual.agent.name
            or current.metadata != actual.metadata
            or {server.name: server.url for server in current.agent.mcp_servers} != urls
        ):
            raise AdmissionDenied(reason="invoker_not_allowed")
        await gate_session_snapshot(
            deps.anthropic.with_options(max_retries=0),
            current,
            deps.tool_safety,
            scope=scope,
            public_url=deps.public_url,
            session_factory=deps.sessionmaker,
            tenant_id=tenant_id,
            entry_point=entry_point,
        )

    return posture, gate
