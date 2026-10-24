"""What a session may still do when nobody is watching the turn.

Every unattended entry point builds its session through this one module, so
the capability surface of a turn nobody watches is decided in one place:

- `require_allowed_capabilities` is a pure check on the session snapshot.
  A coordinator (`multiagent`), skills an entry point does not allow, and any
  unreviewed capability field are refused before anything is sent.
  Managed Agents can neither override `multiagent` at session creation nor
  change it mid-session, so a coordinator session is never repaired, only
  refused.
- `constrain_capabilities` works out the tools and servers to send: built-in
  sandbox tools disabled unless the policy allows them, custom tools always
  removed, an optional explicit tool allowlist, and every MCP toolset gated
  for the unattended decider.
- `invalidate_cached_tools` then `send_capabilities` put it on the session
  (`apply_capabilities` runs both); a caller that restores the session's own
  tools afterwards registers that restore between the two.
  `restore_capabilities` puts them back.
- `capability_posture` makes the decider agree: a built-in call is refused
  when built-ins are off, and a call outside the allowlist is refused.

An allowlist entry is a `ServerTool` pair, never a `server/tool` string to
split: a server named `linear/bridge` would otherwise alias a tool on
`linear`. A server name holding `/` is refused outright.

`ENTRY_POINT_POLICY` records each entry point's skills policy. Built-in
sandbox tools are off except in isolated long-job workers; the admin opt-in is per routine
(`Routine.sandbox_tools_enabled`), and `capability_policy` honours it only for
the `routine` entry point.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any, Final, Literal, NamedTuple, NoReturn, cast, get_args

import structlog
from anthropic import AsyncAnthropic
from anthropic.types.beta import BetaManagedAgentsSession
from anthropic.types.beta.beta_managed_agents_anthropic_skill import (
    BetaManagedAgentsAnthropicSkill,
)
from anthropic.types.beta.beta_managed_agents_custom_skill import BetaManagedAgentsCustomSkill
from anthropic.types.beta.beta_managed_agents_session_agent_update_param import (
    BetaManagedAgentsSessionAgentUpdateParam,
)
from daimon.core.context_prompt import TurnContext
from daimon.core.mcp_server_url import same_mcp_endpoint
from daimon.core.session_ports_compat import update_session_record
from daimon.core.stores.thread_sessions import invalidate_session_tools
from daimon.core.tool_safety import (
    DAIMON_SERVER_NAME,
    SERVER_SEPARATOR,
    WATCH_CONFIRMED_TOOLS,
    ToolCall,
    ToolSafetyPolicy,
    backend_switch_tools,
    heal_reserved_server,
    microsoft_send_tools,
    session_tools_for_policy,
    trusted_servers_for,
)
from daimon.core.turn.approvals import daimon_rule_tools_for
from daimon.core.turn.errors import AdmissionDenied
from daimon.core.turn.posture import PolicyApproval, ToolConfirmationResult
from mux.contracts.ids import Revision, Scope
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

UnattendedEntryPoint = Literal[
    "watch", "routine", "wake", "mcp_chat", "fleet_worker", "cli_run", "checkpoint", "long_job"
]

log = structlog.get_logger(__name__)
_IGNORED_SESSION_FIELDS: set[str] = set()

BUILTIN_TOOLSET: Final[str] = "agent_toolset_20260401"
#: The tools `BUILTIN_TOOLSET` offers, frozen here like the reviewed fields: a
#: built-in a later SDK adds is refused even for an opted-in routine until it
#: is reviewed and listed.
BUILTIN_TOOL_NAMES: Final[frozenset[str]] = frozenset(
    {"bash", "edit", "read", "write", "glob", "grep", "web_fetch", "web_search"}
)
MCP_TOOLSET: Final[str] = "mcp_toolset"
CUSTOM_TOOL: Final[str] = "custom"


class ServerTool(NamedTuple):
    """One MCP tool by its server and its own name, kept apart."""

    server: str
    tool: str


@dataclass(frozen=True)
class CapabilityPolicy:
    """What one unattended entry point may keep beyond gated MCP tools."""

    #: The sandbox toolset (bash, read, write, edit, …).
    allow_builtins: bool
    #: Skills, which cannot be removed from a session once it exists.
    allow_skills: bool


#: Built-ins are on for isolated long-job workers. Only a routine can be
#: opted in, one at a time, by an admin (`capability_policy`).
ENTRY_POINT_POLICY: Final[Mapping[UnattendedEntryPoint, CapabilityPolicy]] = {
    "long_job": CapabilityPolicy(allow_builtins=True, allow_skills=False),
    "watch": CapabilityPolicy(allow_builtins=False, allow_skills=False),
    "routine": CapabilityPolicy(allow_builtins=False, allow_skills=True),
    "wake": CapabilityPolicy(allow_builtins=False, allow_skills=True),
    "mcp_chat": CapabilityPolicy(allow_builtins=False, allow_skills=True),
    "fleet_worker": CapabilityPolicy(allow_builtins=False, allow_skills=True),
    "cli_run": CapabilityPolicy(allow_builtins=False, allow_skills=True),
    "checkpoint": CapabilityPolicy(allow_builtins=False, allow_skills=True),
}

_CLOSED: Final[CapabilityPolicy] = CapabilityPolicy(allow_builtins=False, allow_skills=False)


def capability_policy(
    entry_point: UnattendedEntryPoint | None, *, routine_sandbox_tools: bool = False
) -> CapabilityPolicy:
    """The entry point's policy; a caller that names none gets nothing extra.

    `routine_sandbox_tools` is the routine's own admin opt-in
    (`Routine.sandbox_tools_enabled`). It turns built-ins on for the `routine`
    entry point and is ignored for every other: watches, wakes, agent chat,
    fleet workers, `daimon run` and checkpoints have no opt-in.
    """
    policy = _CLOSED if entry_point is None else ENTRY_POINT_POLICY[entry_point]
    if entry_point == "routine" and routine_sandbox_tools:
        return replace(policy, allow_builtins=True)
    return policy


def unattended_entry_point(origin: TurnContext) -> UnattendedEntryPoint:
    """The entry point of a prepared turn nobody attends, by its origin.

    A fleet worker and a routine are their own; every other prepared
    unattended turn is a timer or a finished worker waking its thread.
    """
    if origin == "worker":
        return "fleet_worker"
    if origin == "routine":
        return "routine"
    return "wake"


def protected_tools(policy: ToolSafetyPolicy) -> frozenset[str]:
    """Daimon's tools that act only on a person's card, for an unattended turn.

    Every one a deployment could offer, whether or not this one does: a
    session that asks on a tool it lacks costs nothing. `microsoft_write`
    asks only with the policy on, as it does in chat.
    """
    return (
        frozenset({"lead_intake_resolve"})
        | WATCH_CONFIRMED_TOOLS
        | backend_switch_tools(enabled=True)
        | microsoft_send_tools(enabled=True, tool_safety_on=policy.enabled)
    )


# Skill kinds are capability variants, so a new kind needs review.
_SKILL_TYPES = frozenset(
    kind
    for model in (BetaManagedAgentsAnthropicSkill, BetaManagedAgentsCustomSkill)
    for kind in get_args(model.model_fields["type"].annotation)
)
_EXECUTION_IDENTITY_TYPES = frozenset({"service_account"})

#: The refusal detail for a coordinator, which delegates to other agents.
COORDINATOR_DETAIL: Final[str] = "delegation to other agents"


def _refuse(detail: str) -> NoReturn:
    log.warning("unattended.capability_refused", detail=detail)
    raise AdmissionDenied(reason="backend_unsupported", detail=detail)


def _field_at(path: str) -> str:
    return f"unreviewed provider field {path}: capability behavior has not been reviewed"


def _value_at(path: str) -> str:
    return f"unreviewed value at {path}"


# One walker checks parsed models and raw provider objects alike. Unknown keys are refused
# only in capability containers. Non-capability nodes and custom input schemas
# are opaque: neither their fields nor their values are checked.
type _Shape = Callable[[object, str], str | None]


def _opaque(_value: object, _path: str) -> str | None:
    return None


def _scalar(*kinds: type) -> _Shape:
    def check(value: object, path: str) -> str | None:
        # `bool` is an `int`; only a field typed bool accepts one.
        fits = value is None or (
            isinstance(value, kinds) and (bool in kinds or not isinstance(value, bool))
        )
        return None if fits else _value_at(path)

    return check


def _exactly(*values: str) -> _Shape:
    """A required tag: one of `values`, never null."""
    return lambda value, path: (
        None
        if isinstance(value, str) and value in values
        else f"unreviewed provider type at {path}: capability behavior has not been reviewed"
    )


def _entries(value: object) -> Mapping[str, object] | None:
    """One node's provider fields, including fields a newer SDK declares."""
    if isinstance(value, BaseModel):
        return {name: getattr(value, name, None) for name in type(value).model_fields} | (
            value.model_extra or {}
        )
    if isinstance(value, dict):
        return cast("dict[str, object]", value)
    return None


def _list(item: _Shape, *, unique: str | None = None, duplicate: str = "") -> _Shape:
    def check(value: object, path: str) -> str | None:
        if not isinstance(value, list):
            return _value_at(path)
        seen: set[object] = set()
        for index, element in enumerate(cast("list[object]", value)):
            if (failed := item(element, f"{path}.{index}")) is not None:
                return failed
            if unique is not None and (entries := _entries(element)) is not None:
                name = entries.get(unique)
                if name in seen:
                    return f"{duplicate} ({path})"
                seen.add(name)
        return None

    return check


def _object(*, strict: bool = True, **fields: _Shape) -> _Shape:
    def check(value: object, path: str) -> str | None:
        entries = _entries(value)
        if entries is None:
            return _value_at(path)
        if unknown := sorted(set(entries) - set(fields)):
            if strict:
                return _field_at(f"{path}.{unknown[0]}")
            for name in unknown:
                if name not in _IGNORED_SESSION_FIELDS:
                    _IGNORED_SESSION_FIELDS.add(name)
                    log.info("unattended.session_field_ignored", field=name)
        for key, check_field in fields.items():
            if (
                key in entries
                and (failed := check_field(entries[key], f"{path}.{key}")) is not None
            ):
                return failed
        return None

    return check


def _requiring(key: str, shape: _Shape) -> _Shape:
    """`shape`, refusing an object without `key` (an object's other keys may be absent)."""

    def check(value: object, path: str) -> str | None:
        if (entries := _entries(value)) is not None:
            if key not in entries:
                return f"missing provider field {path}.{key}"
            return shape(entries, path)
        return shape(value, path)

    return check


def _nullable(shape: _Shape) -> _Shape:
    return lambda value, path: None if value is None else shape(value, path)


def _tagged(**variants: _Shape) -> _Shape:
    """An object whose `type` picks its shape."""

    def check(value: object, path: str) -> str | None:
        entries = _entries(value)
        if entries is None:
            return _value_at(path)
        kind = entries.get("type")
        if not isinstance(kind, str) or kind not in variants:
            return (
                f"unreviewed provider type at {path}.type: "
                "capability behavior has not been reviewed"
            )
        return variants[kind](entries, path)

    return check


_TEXT = _scalar(str)
_TIMESTAMP = _scalar(str, datetime)
_FLAG = _scalar(bool)
_POLICY_SHAPE = _tagged(always_allow=_object(type=_TEXT), always_ask=_object(type=_TEXT))
_DEFAULT_SHAPE = _object(enabled=_FLAG, permission_policy=_POLICY_SHAPE)
_CONFIG_SHAPE = _object(name=_TEXT, enabled=_FLAG, permission_policy=_POLICY_SHAPE)


def _builtin_config(value: object, path: str) -> str | None:
    # A built-in config may echo its own name as `type` (the driver's schema).
    entries = _entries(value)
    if entries is None:
        return _value_at(path)
    config = dict(entries)
    if "type" in config and config.pop("type") != config.get("name"):
        return f"unreviewed provider type at {path}.type: capability behavior has not been reviewed"
    return _CONFIG_SHAPE(config, path)


def _server_name(value: object, path: str) -> str | None:
    if (failed := _TEXT(value, path)) is not None:
        return failed
    if isinstance(value, str) and SERVER_SEPARATOR in value:
        return f"a connection name containing '/': {value} ({path})"
    return None


_SESSION_SHAPE = _object(
    strict=False,
    id=_TEXT,
    type=_TEXT,
    archived_at=_TIMESTAMP,
    budget=_opaque,
    created_at=_TIMESTAMP,
    updated_at=_TIMESTAMP,
    deployment_id=_TEXT,
    environment_id=_TEXT,
    status=_TEXT,
    title=_TEXT,
    metadata=_opaque,
    outcome_evaluations=_opaque,
    stats=_opaque,
    usage=_opaque,
    vault_ids=_list(_TEXT),
    resources=_list(
        _tagged(
            file=_object(
                id=_TEXT,
                type=_TEXT,
                file_id=_TEXT,
                mount_path=_TEXT,
                created_at=_TIMESTAMP,
                updated_at=_TIMESTAMP,
            ),
            github_repository=_object(
                id=_TEXT,
                type=_TEXT,
                url=_TEXT,
                mount_path=_TEXT,
                created_at=_TIMESTAMP,
                updated_at=_TIMESTAMP,
                checkout=_nullable(
                    _tagged(
                        branch=_object(type=_TEXT, name=_TEXT),
                        commit=_object(type=_TEXT, sha=_TEXT),
                    )
                ),
            ),
            memory_store=_object(
                type=_TEXT,
                memory_store_id=_TEXT,
                access=_nullable(_exactly("read_only", "read_write")),
                description=_TEXT,
                instructions=_TEXT,
                mount_path=_TEXT,
                name=_TEXT,
            ),
        )
    ),
    agent=_object(
        id=_TEXT,
        type=_TEXT,
        version=_scalar(int),
        name=_TEXT,
        description=_TEXT,
        system=_TEXT,
        multiagent=_scalar(),
        execution_identity=_nullable(
            _requiring("type", _object(type=_exactly(*_EXECUTION_IDENTITY_TYPES)))
        ),
        model=_opaque,
        skills=_list(
            _object(skill_id=_TEXT, type=_exactly(*_SKILL_TYPES), version=_scalar(str, int))
        ),
        mcp_servers=_list(
            _object(name=_server_name, type=_exactly("url"), url=_TEXT),
            unique="name",
            duplicate="duplicate connection names",
        ),
        tools=_list(
            _tagged(
                agent_toolset_20260401=_object(
                    type=_TEXT,
                    default_config=_DEFAULT_SHAPE,
                    configs=_list(
                        _builtin_config, unique="name", duplicate="duplicate tool configs"
                    ),
                ),
                mcp_toolset=_object(
                    type=_TEXT,
                    mcp_server_name=_server_name,
                    default_config=_DEFAULT_SHAPE,
                    configs=_list(_CONFIG_SHAPE, unique="name", duplicate="duplicate tool configs"),
                ),
                custom=_object(type=_TEXT, name=_TEXT, description=_TEXT, input_schema=_opaque),
            )
        ),
    ),
)


def require_allowed_capabilities(
    snapshot: BetaManagedAgentsSession, *, allow_skills: bool = False
) -> None:
    """Refuse a session whose capabilities cannot be constrained, before any send.

    Pure: reads only `snapshot`. Raises `AdmissionDenied` for a coordinator
    roster, for skills unless `allow_skills`, for a server name holding `/`,
    and for unknown fields in the agent capability container, execution identity,
    tools/configs/policies, MCP servers, skills and resources/files. New session
    fields are ignored and logged once by name; known capability fields retain
    their checks.
    Non-capability parts (budget, outcomes, stats, usage, metadata and model)
    are opaque: neither their keys nor their values are checked. Capability
    values retain their shape checks. The refusal names the field path and
    explains that its capability behavior has not been reviewed.
    """
    if getattr(getattr(snapshot, "agent", None), "multiagent", None) is not None:
        # Named first: a coordinator is refused whatever else it carries.
        _refuse(COORDINATOR_DETAIL)
    if (failed := _SESSION_SHAPE(snapshot, "session")) is not None:
        _refuse(failed)
    if snapshot.agent.skills and not allow_skills:
        _refuse("skills")


@dataclass(frozen=True)
class SessionToolConfig:
    """A session's root `tools` and `mcp_servers`, as SDK param dicts."""

    tools: list[dict[str, Any]]
    mcp_servers: list[dict[str, Any]]

    @classmethod
    def of(cls, snapshot: BetaManagedAgentsSession) -> SessionToolConfig:
        return cls(
            tools=[tool.model_dump(mode="json") for tool in snapshot.agent.tools],
            mcp_servers=[server.model_dump(mode="json") for server in snapshot.agent.mcp_servers],
        )

    def as_update(self) -> BetaManagedAgentsSessionAgentUpdateParam:
        agent: dict[str, Any] = {"tools": self.tools, "mcp_servers": self.mcp_servers}
        return cast(BetaManagedAgentsSessionAgentUpdateParam, agent)


@dataclass(frozen=True)
class ConstrainedSession:
    """The arrays to send, and the session's own arrays to restore afterwards."""

    send: SessionToolConfig
    original: SessionToolConfig
    #: False when `send` equals what the session already carries.
    changed: bool


def _configs(tool: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [dict(config) for config in cast("list[Mapping[str, Any]]", tool.get("configs") or [])]


def _disabled_builtins(tool: dict[str, Any]) -> dict[str, Any]:
    off = {"enabled": False}
    return {
        **tool,
        "default_config": {**(tool.get("default_config") or {}), **off},
        "configs": [{**config, **off} for config in _configs(tool)],
    }


def _asking(tool: dict[str, Any]) -> dict[str, Any]:
    """An MCP toolset whose every call pauses for the unattended decider.

    Trusted, policy-off and per-tool `always_allow` settings all become
    `always_ask`; `enabled` is kept. What the decider then allows (Daimon's
    own ordinary tools, any call with tool safety off) is unchanged, but each
    call is decided, with the agent's rules, channel and trusted hints.
    """
    if tool.get("type") != MCP_TOOLSET:
        return tool
    ask = {"type": "always_ask"}
    return {
        **tool,
        "default_config": {**(tool.get("default_config") or {}), "permission_policy": ask},
        "configs": [{**config, "permission_policy": ask} for config in _configs(tool)],
    }


def _allowlisted(
    tools: list[dict[str, Any]],
    servers: list[dict[str, Any]],
    allowlist: frozenset[ServerTool],
    *,
    public_url: str | None,
    own_reads: frozenset[ServerTool],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Only the MCP tools `allowlist` names, each enabled alone.

    An entry whose server name holds `/` is refused rather than skipped: a
    caller that built one has already confused a composite key for a server.

    Daimon's own management servers are removed unless an allowlisted pair
    is also in the caller's verified `own_reads`: `daimon-mcp`, `sandbox`,
    and any URL alias of the deployment endpoint (`same_mcp_endpoint`).
    """
    own = {DAIMON_SERVER_NAME, "sandbox"} | {
        s["name"]
        for s in servers
        if public_url and isinstance(s.get("url"), str) and same_mcp_endpoint(s["url"], public_url)
    }
    wanted: dict[str, list[str]] = {}
    for server, name in sorted(allowlist):
        if SERVER_SEPARATOR in server:
            _refuse(f"a server name holding '/' (tool allowlist entry {server!r})")
        if server and name and (server not in own or ServerTool(server, name) in own_reads):
            wanted.setdefault(server, []).append(name)
    kept: list[dict[str, Any]] = []
    for tool in tools:
        if tool.get("type") != MCP_TOOLSET:
            if tool.get("type") == BUILTIN_TOOLSET:
                kept.append(tool)
            continue
        names = wanted.get(tool.get("mcp_server_name") or "")
        if not names:
            continue
        default_config = dict(tool.get("default_config") or {})
        configs = {c.get("name"): c for c in _configs(tool)}
        kept.append(
            {
                **tool,
                "default_config": {**default_config, "enabled": False},
                "configs": [
                    {**default_config, **configs.get(name, {}), "name": name, "enabled": True}
                    for name in names
                ],
            }
        )
    named = {t.get("mcp_server_name") for t in kept if t.get("type") == MCP_TOOLSET}
    return kept, [s for s in servers if s["name"] in named]


async def constrain_capabilities(
    snapshot: BetaManagedAgentsSession,
    policy: ToolSafetyPolicy,
    *,
    public_url: str | None,
    session_factory: async_sessionmaker[AsyncSession] | None,
    tenant_id: uuid.UUID | None,
    allow_builtins: bool = False,
    allow_skills: bool = False,
    tool_allowlist: frozenset[ServerTool] | None = None,
) -> ConstrainedSession:
    """The tools and servers `snapshot` may run with unattended. No provider call.

    Refuses first (`require_allowed_capabilities`). Built-in tools are
    disabled unless `allow_builtins`, and custom tools are always removed;
    with a `tool_allowlist` of
    `ServerTool` pairs only those MCP tools stay, and every server no kept
    toolset names is dropped. Every MCP toolset left, Daimon's own included and
    with tool safety on or off, asks on its default and on every per-tool
    config, so each call reaches the unattended decider; Daimon's
    `protected_tools` and rule tools are listed as `session_tools_for_policy`
    lists them, and a foreign `daimon-mcp` URL is healed.
    """
    require_allowed_capabilities(snapshot, allow_skills=allow_skills)
    original = SessionToolConfig.of(snapshot)
    tools = [dict(tool) for tool in original.tools]
    servers = [dict(server) for server in original.mcp_servers]
    tools = [
        _disabled_builtins(tool)
        if tool.get("type") == BUILTIN_TOOLSET and not allow_builtins
        else tool
        for tool in tools
        if tool.get("type") != CUSTOM_TOOL
    ]
    # Only this read on the verified deployment endpoint may retain own-server access.
    own_reads = frozenset(
        ServerTool(server["name"], "get_long_job")
        for server in servers
        if server["name"] == DAIMON_SERVER_NAME
        and public_url is not None
        and isinstance(server.get("url"), str)
        and same_mcp_endpoint(server["url"], public_url)
    )
    if tool_allowlist is not None:
        tools, servers = _allowlisted(
            tools, servers, tool_allowlist, public_url=public_url, own_reads=own_reads
        )
    rules = (
        await daimon_rule_tools_for(
            session_factory, policy, tenant_id=tenant_id, agent_id=snapshot.agent.id
        )
        if session_factory is not None
        else frozenset[str]()
    )
    protected = protected_tools(policy)
    gated = session_tools_for_policy(
        policy,
        tools,
        trusted_servers=trusted_servers_for(public_url),
        confirmed_tools=protected,
        daimon_rule_tools=rules,
    )
    healed = heal_reserved_server(policy, servers, public_url=public_url, confirmed_tools=protected)
    send = SessionToolConfig(
        tools=[_asking(tool) for tool in (gated if gated is not None else tools)],
        mcp_servers=healed if healed is not None else servers,
    )
    # Fixed confirmation gates may append their own enabled configurations.
    # Reapply the explicit authority boundary after those rewrites.
    if tool_allowlist is not None:
        kept_tools, kept_servers = _allowlisted(
            send.tools, send.mcp_servers, tool_allowlist, public_url=public_url, own_reads=own_reads
        )
        send = SessionToolConfig(tools=kept_tools, mcp_servers=kept_servers)
    return ConstrainedSession(send=send, original=original, changed=send != original)


async def invalidate_cached_tools(
    snapshot: BetaManagedAgentsSession,
    constrained: ConstrainedSession,
    *,
    session_factory: async_sessionmaker[AsyncSession] | None,
) -> None:
    """Clear and commit the thread session's cached tool hashes, if `constrained` changes anything.

    `invalidate_session_tools` keeps the session's identity, workspace and
    credential metadata, so the next ordinary bind reapplies its own tools
    even if this turn never gets to restore them. No provider call; without
    a `session_factory` it does nothing.
    """
    if not constrained.changed or session_factory is None:
        return
    async with session_factory.begin() as db:
        await invalidate_session_tools(db, ma_session_id=snapshot.id)


async def send_capabilities(
    client: AsyncAnthropic,
    snapshot: BetaManagedAgentsSession,
    constrained: ConstrainedSession,
    *,
    scope: Scope,
) -> None:
    """Send `constrained` to the session, when it changes anything. A failure raises.

    Call `invalidate_cached_tools` first. A caller that restores the
    session's own tools afterwards registers that restore between the two,
    so a send that fails, or is cancelled, part-way is still restored.
    """
    if not constrained.changed:
        return
    await _update(client, snapshot, constrained.send, scope=scope)


async def apply_capabilities(
    client: AsyncAnthropic,
    snapshot: BetaManagedAgentsSession,
    constrained: ConstrainedSession,
    *,
    scope: Scope,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> None:
    """`invalidate_cached_tools`, then `send_capabilities`, with nothing between.

    For a caller that never restores the session's own tools; one that does
    calls the two itself.
    """
    await invalidate_cached_tools(snapshot, constrained, session_factory=session_factory)
    await send_capabilities(client, snapshot, constrained, scope=scope)


async def restore_capabilities(
    client: AsyncAnthropic,
    snapshot: BetaManagedAgentsSession,
    original: SessionToolConfig,
    *,
    scope: Scope,
) -> None:
    """Put `original` back, finishing even when the turn is cancelled meanwhile.

    Shutdown and an owner stop can both cancel the same turn; the restore is
    shielded again on every request, and the cancellation propagates only
    after it.
    """
    restore = asyncio.create_task(_update(client, snapshot, original, scope=scope))
    cancellation: asyncio.CancelledError | None = None
    while True:
        try:
            await asyncio.shield(restore)
            break
        except asyncio.CancelledError as exc:
            if restore.cancelled():
                raise
            cancellation = exc
    if cancellation is not None:
        raise cancellation


async def _update(
    client: AsyncAnthropic,
    snapshot: BetaManagedAgentsSession,
    config: SessionToolConfig,
    *,
    scope: Scope,
) -> None:
    await update_session_record(
        client,
        snapshot.id,
        scope=scope,
        agent_id=snapshot.agent.id,
        revision=Revision(local=snapshot.agent.version, native=str(snapshot.agent.version)),
        agent=config.as_update(),
    )


def capability_posture(
    posture: PolicyApproval,
    *,
    allow_builtins: bool = False,
    tool_allowlist: frozenset[ServerTool] | None = None,
) -> PolicyApproval:
    """`posture`, refusing what `constrain_capabilities` took out of the session.

    A disabled tool should never pause, so this is the second lock: a
    built-in or custom call (`server_name=None`) is refused unless
    `allow_builtins` (a routine an admin opted in) and it names a reviewed
    built-in (`BUILTIN_TOOL_NAMES`), so a custom tool's call is refused even
    then. With a `tool_allowlist` so is every unlisted call.
    """
    decide = posture.decide

    async def constrained(call: ToolCall) -> ToolConfirmationResult:
        if call.server_name is None and (
            not allow_builtins or call.tool_name not in BUILTIN_TOOL_NAMES
        ):
            return ToolConfirmationResult(
                allow=False,
                deny_message=(
                    f"Refused {call.tool_name}: built-in sandbox tools are off for this "
                    "unattended reply. The tool call did not run."
                ),
            )
        if (
            call.server_name is not None
            and tool_allowlist is not None
            and ServerTool(call.server_name, call.tool_name) not in tool_allowlist
        ):
            return ToolConfirmationResult(
                allow=False,
                deny_message=(
                    f"Refused {call.key}: this unattended reply may only use its listed "
                    "tools. The tool call did not run."
                ),
            )
        return await decide(call)

    return PolicyApproval(decide=constrained)
