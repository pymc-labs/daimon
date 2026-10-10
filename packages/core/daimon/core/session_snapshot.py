"""The effective configuration a live MA session is actually running.

An MA session freezes its agent at creation time: model, system prompt, skills,
agent version, environment, repo checkout, memory store and vault are a
snapshot, and later `agents.update` calls never reach a session that already
exists. Only `agent.tools`, `agent.mcp_servers` and file resources can change in
place. `SessionSnapshot` records that frozen configuration so a later turn can
compare what the session runs against what the caller's configuration now
wants, without re-reading the session from MA.

Two axes, because they have different consequences:

- **identity** — changing any of it requires a *replacement* session.
- **mutable** — changing any of it can be applied to the live session.

`fingerprint_identity` / `fingerprint_mutable` reduce each axis to one hex
string. The handle fields (resource/file ids) and the diagnostics
(`agent_version`, `agent_name`) are deliberately excluded from both: they
identify or describe, they are not configuration, and a rotated handle must not
read as a configuration change.

Pure module — no I/O, no clock. Hashes are sha256 over canonical JSON, and
every list is sorted by its own canonical JSON before hashing so that a
reordered `tools` array is not a change.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from typing import Literal, cast

from anthropic.types.beta import BetaManagedAgentsAgent, BetaManagedAgentsSession
from anthropic.types.beta.beta_managed_agents_agent_toolset20260401 import (
    BetaManagedAgentsAgentToolset20260401,
)
from anthropic.types.beta.beta_managed_agents_anthropic_skill import BetaManagedAgentsAnthropicSkill
from anthropic.types.beta.beta_managed_agents_branch_checkout import (
    BetaManagedAgentsBranchCheckout,
)
from anthropic.types.beta.beta_managed_agents_custom_skill import BetaManagedAgentsCustomSkill
from anthropic.types.beta.beta_managed_agents_custom_tool import BetaManagedAgentsCustomTool
from anthropic.types.beta.beta_managed_agents_mcp_server_url_definition import (
    BetaManagedAgentsMCPServerURLDefinition,
)
from anthropic.types.beta.beta_managed_agents_mcp_toolset import BetaManagedAgentsMCPToolset
from anthropic.types.beta.sessions.beta_managed_agents_file_resource import (
    BetaManagedAgentsFileResource,
)
from anthropic.types.beta.sessions.beta_managed_agents_github_repository_resource import (
    BetaManagedAgentsGitHubRepositoryResource,
)
from daimon.core.mcp_personal_servers import visible_mcp_servers, visible_tools
from daimon.core.tool_safety import (
    OPEN_TOOL_SAFETY,
    ToolSafetyPolicy,
    heal_reserved_server,
    session_tools_for_policy,
    trusted_servers_for,
)
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

type JsonValue = str | int | float | bool | None | list[JsonValue] | dict[str, JsonValue]
type MaSkill = BetaManagedAgentsAnthropicSkill | BetaManagedAgentsCustomSkill
type MaTool = (
    BetaManagedAgentsAgentToolset20260401
    | BetaManagedAgentsMCPToolset
    | BetaManagedAgentsCustomTool
)

_ENV_MOUNT_SUFFIX = ".env"
_TOOLS: TypeAdapter[list[MaTool]] = TypeAdapter(list[MaTool])
_MCP_SERVERS: TypeAdapter[list[BetaManagedAgentsMCPServerURLDefinition]] = TypeAdapter(
    list[BetaManagedAgentsMCPServerURLDefinition]
)

_IDENTITY_FIELDS = (
    "schema_version",
    "ma_agent_id",
    "model_id",
    "system_sha256",
    "skills_sha256",
    "environment_id",
    "repo_url",
    "repo_branch",
    "memory_store_id",
    "memory_read_only",
    "vault_id",
)
_MUTABLE_FIELDS = ("schema_version", "tools_sha256", "mcp_servers_sha256", "env_sha256")


class SessionSnapshot(BaseModel):
    """What one MA session is running. Persisted as JSONB on `thread_sessions`."""

    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = 1

    # Identity axis — a difference here can only be applied by replacing the session.
    ma_agent_id: str
    model_id: str
    system_sha256: str | None
    skills_sha256: str
    environment_id: str
    github_mode: Literal["legacy", "app"] = "legacy"
    repo_urls: tuple[str, ...] = ()
    repo_url: str | None
    repo_branch: str | None
    memory_store_id: str | None
    memory_read_only: bool = False
    vault_id: str | None

    # Mutable axis — a difference here can be applied to the live session.
    tools_sha256: str
    mcp_servers_sha256: str
    env_sha256: str | None

    # Handles: what to call to apply a mutable change. Never fingerprinted.
    env_file_id: str | None = None
    env_resource_id: str | None = None
    repo_resource_id: str | None = None
    repo_resource_ids: dict[str, str] = Field(default_factory=dict)
    repo_mount_path: str | None = None
    repo_token_issued_at: int | None = None

    # Diagnostics only. Never fingerprinted — an agent version bump on its own
    # is not a configuration change to the session that already froze it.
    agent_version: int
    agent_name: str


def canonical_json(obj: JsonValue) -> str:
    """One byte-stable JSON encoding: sorted keys, no insignificant whitespace."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _sha256_of(payload: str) -> str:
    return hashlib.sha256(payload.encode()).hexdigest()


def _hash_model_list(items: Sequence[BaseModel]) -> str:
    """sha256 over the canonical JSON of the items, sorted by their own encoding.

    Sorting by encoding rather than by a per-type key means a list whose members
    were merely reordered hashes the same, without this module having to know
    which field identifies each union member.
    """
    dumped: list[JsonValue] = [item.model_dump(mode="json") for item in items]
    return _sha256_of(canonical_json(sorted(dumped, key=canonical_json)))


def hash_system(system: str | None) -> str | None:
    """None for no system prompt — distinct from the hash of an empty prompt."""
    return None if system is None else _sha256_of(system)


def hash_skills(skills: Sequence[MaSkill]) -> str:
    return _hash_model_list(skills)


def hash_tools(tools: Sequence[MaTool]) -> str:
    return _hash_model_list(tools)


def hash_mcp_servers(mcp_servers: Sequence[BetaManagedAgentsMCPServerURLDefinition]) -> str:
    return _hash_model_list(mcp_servers)


def hash_env_bytes(content: bytes) -> str:
    """Hash the exact bytes `credential_env.assemble_env_bytes` produces."""
    return hashlib.sha256(content).hexdigest()


def fingerprint_identity(snapshot: SessionSnapshot) -> str:
    fields = _IDENTITY_FIELDS
    if snapshot.github_mode == "app":
        fields = (*fields, "github_mode", "repo_urls")
    return _fingerprint(snapshot, fields)


def fingerprint_mutable(snapshot: SessionSnapshot) -> str:
    return _fingerprint(snapshot, _MUTABLE_FIELDS)


def _fingerprint(snapshot: SessionSnapshot, fields: Sequence[str]) -> str:
    dumped: dict[str, JsonValue] = snapshot.model_dump(mode="json")
    return _sha256_of(canonical_json({name: dumped[name] for name in fields}))


def snapshot_from_created_session(
    session: BetaManagedAgentsSession,
    *,
    env_sha256: str | None,
    env_file_id: str | None,
    repo_token_issued_at: int | None,
    vault_id: str | None,
    sent_skills: Sequence[MaSkill],
    github_mode: Literal["legacy", "app"] = "legacy",
) -> SessionSnapshot:
    """Snapshot a session we just created, from the response plus what we sent.

    `env_sha256` and `repo_token_issued_at` are not readable back off the
    session — only the creator knows the bytes it uploaded and when it minted
    the clone token — so the caller supplies them.

    `sent_skills` are hashed instead of the session's own: MA echoes a skill
    sent as `version="latest"` back at the version it resolved, which the
    desired snapshot (built from the agent's `"latest"`) would read as drift
    on every turn after.
    """
    return _snapshot_from_session(
        session,
        skills_sha256=hash_skills(sent_skills),
        env_sha256=env_sha256,
        env_file_id=env_file_id,
        repo_token_issued_at=repo_token_issued_at,
        vault_id=vault_id,
        github_mode=github_mode,
    )


def snapshot_from_retrieved_session(
    session: BetaManagedAgentsSession,
    *,
    env_sha256: str | None = None,
    repo_token_issued_at: int | None = None,
    vault_id: str | None = None,
) -> SessionSnapshot:
    """Snapshot a session created before snapshots were persisted (backfill).

    Everything the session itself reports is authoritative; `env_sha256` and
    `repo_token_issued_at` default to None because a row written before this
    existed has no record of them, which reads downstream as "unknown, refresh".
    """
    return _snapshot_from_session(
        session,
        skills_sha256=hash_skills(session.agent.skills),
        env_sha256=env_sha256,
        env_file_id=None,
        repo_token_issued_at=repo_token_issued_at,
        vault_id=vault_id,
    )


def _snapshot_from_session(
    session: BetaManagedAgentsSession,
    *,
    skills_sha256: str,
    env_sha256: str | None,
    env_file_id: str | None,
    repo_token_issued_at: int | None,
    vault_id: str | None,
    github_mode: Literal["legacy", "app"] = "legacy",
) -> SessionSnapshot:
    env_resource_id: str | None = None
    resolved_env_file_id = env_file_id
    repo_resource_id: str | None = None
    repo_resource_ids: dict[str, str] = {}
    repo_urls: list[str] = []
    repo_url: str | None = None
    repo_branch: str | None = None
    repo_mount_path: str | None = None
    memory_store_id: str | None = None
    memory_read_only = False

    for resource in session.resources:
        if isinstance(resource, BetaManagedAgentsFileResource):
            if resource.mount_path.endswith(_ENV_MOUNT_SUFFIX):
                env_resource_id = resource.id
                resolved_env_file_id = resolved_env_file_id or resource.file_id
        elif isinstance(resource, BetaManagedAgentsGitHubRepositoryResource):
            repo_urls.append(resource.url)
            resource_id = cast(str | None, resource.id)
            if resource_id is not None:
                repo_resource_ids[resource.url] = resource_id
            repo_resource_id = resource.id
            repo_url = resource.url
            repo_mount_path = resource.mount_path
            if isinstance(resource.checkout, BetaManagedAgentsBranchCheckout):
                repo_branch = resource.checkout.name
        else:
            memory_store_id = resource.memory_store_id
            memory_read_only = resource.access == "read_only"

    agent = session.agent
    return SessionSnapshot(
        ma_agent_id=agent.id,
        model_id=agent.model.id,
        system_sha256=hash_system(agent.system),
        skills_sha256=skills_sha256,
        environment_id=session.environment_id,
        github_mode=github_mode,
        repo_urls=tuple(sorted(repo_urls)) if github_mode == "app" else (),
        repo_url=None if github_mode == "app" else repo_url,
        repo_branch=None if github_mode == "app" else repo_branch,
        memory_store_id=memory_store_id,
        memory_read_only=memory_read_only,
        vault_id=vault_id if vault_id is not None else next(iter(session.vault_ids), None),
        tools_sha256=hash_tools(agent.tools),
        mcp_servers_sha256=hash_mcp_servers(agent.mcp_servers),
        env_sha256=env_sha256,
        env_file_id=resolved_env_file_id,
        env_resource_id=env_resource_id,
        repo_resource_id=repo_resource_id,
        repo_resource_ids=repo_resource_ids,
        repo_mount_path=repo_mount_path,
        repo_token_issued_at=repo_token_issued_at,
        agent_version=agent.version,
        agent_name=agent.name,
    )


def session_skills(
    agent: BetaManagedAgentsAgent, channel_skills: Sequence[BetaManagedAgentsCustomSkill] = ()
) -> list[MaSkill]:
    """The skills a session runs: the agent's own, then its channel's extra ones.

    The one definition `create_session` and the drift check both use, so a
    session started with a channel's skills reads as current on its next turn.
    The channel's are decided at admission (`daimon.core.channel_skills`).
    """
    return [*agent.skills, *channel_skills]


def session_tools(
    agent: BetaManagedAgentsAgent,
    hidden_mcp_server_names: frozenset[str],
    *,
    tool_safety: ToolSafetyPolicy,
    public_url: str | None,
    asks_before_publishing: bool = False,
) -> Sequence[MaTool]:
    """The tools a session for this caller runs: the visible ones, tool safety applied,
    and the publish tools asking first when the turn's agent may not publish freely.

    The one definition `create_session`, the bind-time drift check and the
    in-place update all use. Hashing or pushing the agent's raw tools instead
    read every gated session as drifted and wrote `always_allow` back onto it
    on its next turn, so third-party writes stopped asking after the first.
    """
    visible = visible_tools(agent, hidden_mcp_server_names)
    gated = session_tools_for_policy(
        tool_safety,
        [tool.model_dump(mode="json") for tool in visible],
        trusted_servers=trusted_servers_for(public_url),
        asks_before_publishing=asks_before_publishing,
    )
    return visible if gated is None else _TOOLS.validate_python(gated)


def session_mcp_servers(
    agent: BetaManagedAgentsAgent,
    hidden_mcp_server_names: frozenset[str],
    *,
    tool_safety: ToolSafetyPolicy,
    public_url: str | None,
) -> Sequence[BetaManagedAgentsMCPServerURLDefinition]:
    """The MCP servers a session for this caller runs, a foreign `daimon-mcp` re-pointed."""
    visible = visible_mcp_servers(agent, hidden_mcp_server_names)
    healed = heal_reserved_server(
        tool_safety, [server.model_dump(mode="json") for server in visible], public_url=public_url
    )
    return visible if healed is None else _MCP_SERVERS.validate_python(healed)


def desired_snapshot(
    agent: BetaManagedAgentsAgent,
    *,
    hidden_mcp_server_names: frozenset[str],
    environment_id: str,
    env_sha256: str | None,
    repo_url: str | None,
    github_mode: Literal["legacy", "app"] = "legacy",
    repo_urls: tuple[str, ...] = (),
    repo_branch: str | None,
    memory_store_id: str | None,
    vault_id: str | None,
    env_file_id: str | None = None,
    memory_read_only: bool = False,
    asks_before_publishing: bool = False,
    repo_mount_path: str | None = None,
    repo_token_issued_at: int | None = None,
    tool_safety: ToolSafetyPolicy = OPEN_TOOL_SAFETY,
    public_url: str | None = None,
    channel_skills: Sequence[BetaManagedAgentsCustomSkill] = (),
) -> SessionSnapshot:
    """What a session created right now, for this caller, would be running.

    Resource-id handles are None: a desired configuration has no resources yet.
    Compare this against a recorded snapshot's fingerprints, never field by
    field against its handles.

    `hidden_mcp_server_names` is what `create_session` would leave out of this
    caller's session — the servers only somebody else's OAuth grant can
    authenticate. Both arrays are hashed after that cut, or a session created
    with those overrides reads as drifted on every turn and has the hidden
    servers pushed straight back onto it. `tool_safety` and `public_url` are
    the deployment's, for the same reason: both arrays are hashed as
    `create_session` gates them (`session_tools`). `channel_skills` are the
    turn's channel's extra skills, hashed with the agent's (`session_skills`).
    """
    return SessionSnapshot(
        ma_agent_id=agent.id,
        model_id=agent.model.id,
        system_sha256=hash_system(agent.system),
        skills_sha256=hash_skills(session_skills(agent, channel_skills)),
        environment_id=environment_id,
        github_mode=github_mode,
        repo_urls=repo_urls if github_mode == "app" else (),
        repo_url=repo_url,
        repo_branch=repo_branch,
        memory_store_id=memory_store_id,
        # No mount has no writable memory. Match the observed snapshot so a
        # provisioning outage does not cause a replacement on every turn.
        memory_read_only=memory_read_only and memory_store_id is not None,
        vault_id=vault_id,
        tools_sha256=hash_tools(
            session_tools(
                agent,
                hidden_mcp_server_names,
                tool_safety=tool_safety,
                public_url=public_url,
                asks_before_publishing=asks_before_publishing,
            )
        ),
        mcp_servers_sha256=hash_mcp_servers(
            session_mcp_servers(
                agent, hidden_mcp_server_names, tool_safety=tool_safety, public_url=public_url
            )
        ),
        env_sha256=env_sha256,
        env_file_id=env_file_id,
        env_resource_id=None,
        repo_resource_id=None,
        repo_mount_path=repo_mount_path,
        repo_token_issued_at=repo_token_issued_at,
        agent_version=agent.version,
        agent_name=agent.name,
    )
