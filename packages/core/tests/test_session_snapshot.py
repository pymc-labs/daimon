"""What a live MA session is running, reduced to two comparable fingerprints.

Every SDK object here is constructed inline with the real constructor: when the
SDK adds a required field these break, which is the point — a snapshot that
silently stopped covering a field would read as "no configuration change" and
keep a caller on a stale session forever.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from anthropic.types.beta import (
    BetaManagedAgentsAgent,
    BetaManagedAgentsSession,
    BetaManagedAgentsSessionAgent,
)
from anthropic.types.beta.beta_managed_agents_anthropic_skill import BetaManagedAgentsAnthropicSkill
from anthropic.types.beta.beta_managed_agents_branch_checkout import (
    BetaManagedAgentsBranchCheckout,
)
from anthropic.types.beta.beta_managed_agents_custom_skill import BetaManagedAgentsCustomSkill
from anthropic.types.beta.beta_managed_agents_custom_tool import BetaManagedAgentsCustomTool
from anthropic.types.beta.beta_managed_agents_custom_tool_input_schema import (
    BetaManagedAgentsCustomToolInputSchema,
)
from anthropic.types.beta.beta_managed_agents_mcp_server_url_definition import (
    BetaManagedAgentsMCPServerURLDefinition,
)
from anthropic.types.beta.beta_managed_agents_model_config import BetaManagedAgentsModelConfig
from anthropic.types.beta.beta_managed_agents_session_stats import BetaManagedAgentsSessionStats
from anthropic.types.beta.beta_managed_agents_session_usage import BetaManagedAgentsSessionUsage
from anthropic.types.beta.sessions.beta_managed_agents_file_resource import (
    BetaManagedAgentsFileResource,
)
from anthropic.types.beta.sessions.beta_managed_agents_github_repository_resource import (
    BetaManagedAgentsGitHubRepositoryResource,
)
from anthropic.types.beta.sessions.beta_managed_agents_memory_store_resource import (
    BetaManagedAgentsMemoryStoreResource,
)
from daimon.core.credential_env import assemble_env_bytes
from daimon.core.session_compat import identity_change_reasons
from daimon.core.session_snapshot import (
    SessionSnapshot,
    desired_snapshot,
    fingerprint_identity,
    fingerprint_mutable,
    hash_env_bytes,
    hash_mcp_servers,
    hash_skills,
    hash_system,
    hash_tools,
    session_skills,
    snapshot_from_created_session,
)
from daimon.core.stores.domain import AgentFileRow

_NOW = datetime(2026, 9, 13, 12, 0, tzinfo=UTC)


def _agent(
    *,
    model_id: str = "claude-sonnet-5",
    system: str | None = "be useful",
) -> BetaManagedAgentsAgent:
    """The one shared piece: an agent with nothing configured but model + prompt.

    Tests that care about skills/tools/mcp_servers build their own agent inline
    so the thing under test is visible at its call site.
    """
    return BetaManagedAgentsAgent(
        id="agent_research",
        archived_at=None,
        created_at=_NOW,
        description=None,
        mcp_servers=[],
        metadata={},
        model=BetaManagedAgentsModelConfig(id=model_id),
        name="research-bot",
        skills=[],
        system=system,
        tools=[],
        type="agent",
        updated_at=_NOW,
        version=3,
    )


def test_fingerprints_are_identical_when_the_same_configuration_is_rebuilt() -> None:
    first = desired_snapshot(
        _agent(),
        hidden_mcp_server_names=frozenset(),
        environment_id="env_science",
        env_sha256="abc",
        repo_url=None,
        repo_branch=None,
        memory_store_id="memstore_1",
        vault_id="vault_1",
    )
    second = desired_snapshot(
        _agent(),
        hidden_mcp_server_names=frozenset(),
        environment_id="env_science",
        env_sha256="abc",
        repo_url=None,
        repo_branch=None,
        memory_store_id="memstore_1",
        vault_id="vault_1",
    )

    assert fingerprint_identity(first) == fingerprint_identity(second), (
        "the same configuration must fingerprint the same, or every bind replaces the session"
    )
    assert fingerprint_mutable(first) == fingerprint_mutable(second), (
        "the same mutable axis must fingerprint the same across rebuilds"
    )


def test_skills_hash_is_unchanged_when_the_skill_list_is_reordered() -> None:
    pdf = BetaManagedAgentsAnthropicSkill(skill_id="skill_pdf", type="anthropic", version="1")
    xlsx = BetaManagedAgentsAnthropicSkill(skill_id="skill_xlsx", type="anthropic", version="2")

    assert hash_skills([pdf, xlsx]) == hash_skills([xlsx, pdf]), (
        "reordering a skill list is not a configuration change"
    )


def test_tools_and_mcp_server_hashes_are_unchanged_when_their_lists_are_reordered() -> None:
    search = BetaManagedAgentsCustomTool(
        description="search the corpus",
        input_schema=BetaManagedAgentsCustomToolInputSchema(type="object"),
        name="search",
        type="custom",
    )
    summarize = BetaManagedAgentsCustomTool(
        description="summarize a document",
        input_schema=BetaManagedAgentsCustomToolInputSchema(type="object"),
        name="summarize",
        type="custom",
    )
    linear = BetaManagedAgentsMCPServerURLDefinition(
        name="linear", type="url", url="https://mcp.example/linear"
    )
    notion = BetaManagedAgentsMCPServerURLDefinition(
        name="notion", type="url", url="https://mcp.example/notion"
    )

    assert hash_tools([search, summarize]) == hash_tools([summarize, search]), (
        "reordering a tool list is not a configuration change"
    )
    assert hash_mcp_servers([linear, notion]) == hash_mcp_servers([notion, linear]), (
        "reordering an mcp_servers list is not a configuration change"
    )


def test_changing_one_tool_changes_the_tools_hash() -> None:
    original = BetaManagedAgentsCustomTool(
        description="search the corpus",
        input_schema=BetaManagedAgentsCustomToolInputSchema(type="object"),
        name="search",
        type="custom",
    )
    reworded = BetaManagedAgentsCustomTool(
        description="search the corpus, including drafts",
        input_schema=BetaManagedAgentsCustomToolInputSchema(type="object"),
        name="search",
        type="custom",
    )

    assert hash_tools([original]) != hash_tools([reworded]), (
        "an edited tool definition must read as a change, or it never reaches the session"
    )


def test_env_hash_matches_the_bytes_the_credential_assembler_produces() -> None:
    tenant_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    rows = [
        AgentFileRow(
            tenant_id=tenant_id,
            agent_id=agent_id,
            key="OPENAI_API_KEY",
            content="sk-test",
            created_at=_NOW,
            updated_at=_NOW,
        ),
        AgentFileRow(
            tenant_id=tenant_id,
            agent_id=agent_id,
            key="TOGGL_TOKEN",
            content="tok",
            created_at=_NOW,
            updated_at=_NOW,
        ),
    ]

    snapshot = desired_snapshot(
        _agent(),
        hidden_mcp_server_names=frozenset(),
        environment_id="env_science",
        env_sha256=hash_env_bytes(assemble_env_bytes(rows)),
        repo_url=None,
        repo_branch=None,
        memory_store_id=None,
        vault_id=None,
    )

    assert snapshot.env_sha256 == hash_env_bytes(assemble_env_bytes(rows)), (
        "the env hash must be over exactly the bytes that get mounted, not a derived form"
    )
    assert snapshot.env_sha256 != hash_env_bytes(assemble_env_bytes(rows[:1])), (
        "removing a key must change the env hash, or a key removal never reaches the session"
    )


def test_absent_system_prompt_hashes_differently_from_an_empty_one() -> None:
    assert hash_system(None) is None, "no system prompt has no hash, it is not a hash of nothing"
    assert hash_system("") is not None, "an empty prompt is a prompt and must hash"

    absent = fingerprint_identity(
        desired_snapshot(
            _agent(system=None),
            hidden_mcp_server_names=frozenset(),
            environment_id="env_science",
            env_sha256=None,
            repo_url=None,
            repo_branch=None,
            memory_store_id=None,
            vault_id=None,
        )
    )
    empty = fingerprint_identity(
        desired_snapshot(
            _agent(system=""),
            hidden_mcp_server_names=frozenset(),
            environment_id="env_science",
            env_sha256=None,
            repo_url=None,
            repo_branch=None,
            memory_store_id=None,
            vault_id=None,
        )
    )

    assert absent != empty, "clearing a system prompt must be visible as a configuration change"


def test_snapshot_from_created_session_captures_every_resource_handle() -> None:
    session = BetaManagedAgentsSession(
        id="sess_abc",
        agent=BetaManagedAgentsSessionAgent(
            id="agent_research",
            description=None,
            mcp_servers=[
                BetaManagedAgentsMCPServerURLDefinition(
                    name="linear", type="url", url="https://mcp.example/linear"
                )
            ],
            model=BetaManagedAgentsModelConfig(id="claude-sonnet-5"),
            name="research-bot",
            skills=[
                BetaManagedAgentsAnthropicSkill(skill_id="skill_pdf", type="anthropic", version="1")
            ],
            system="be useful",
            tools=[],
            type="agent",
            version=3,
        ),
        archived_at=None,
        created_at=_NOW,
        environment_id="env_science",
        metadata={"daimon_tenant": "t"},
        outcome_evaluations=[],
        resources=[
            BetaManagedAgentsFileResource(
                id="res_env",
                created_at=_NOW,
                file_id="file_env",
                mount_path="/mnt/session/uploads/.env",
                type="file",
                updated_at=_NOW,
            ),
            BetaManagedAgentsGitHubRepositoryResource(
                id="res_repo",
                created_at=_NOW,
                mount_path="/mnt/session/repo",
                type="github_repository",
                updated_at=_NOW,
                url="https://github.com/pymc-labs/example",
                checkout=BetaManagedAgentsBranchCheckout(name="feat/thing", type="branch"),
            ),
            BetaManagedAgentsMemoryStoreResource(
                memory_store_id="memstore_7",
                type="memory_store",
                access="read_write",
                mount_path="/mnt/memory/research-bot",
            ),
        ],
        stats=BetaManagedAgentsSessionStats(),
        status="idle",
        title=None,
        type="session",
        updated_at=_NOW,
        usage=BetaManagedAgentsSessionUsage(),
        vault_ids=["vault_9"],
    )

    snapshot = snapshot_from_created_session(
        session,
        env_sha256="env-hash",
        env_file_id="file_env",
        repo_token_issued_at=1789000000,
        vault_id=None,
        sent_skills=session.agent.skills,
    )

    assert snapshot.env_resource_id == "res_env", "the .env resource id is the handle to replace it"
    assert snapshot.env_file_id == "file_env", "the uploaded file id must be recorded"
    assert snapshot.repo_resource_id == "res_repo", (
        "the repo resource id is the token-rotation handle"
    )
    assert snapshot.repo_url == "https://github.com/pymc-labs/example", (
        "repo url comes off the resource"
    )
    assert snapshot.repo_urls == (), "legacy snapshots keep the predeploy repo-set default"
    assert snapshot.repo_branch == "feat/thing", "a branch checkout must be read off the resource"
    assert snapshot.repo_mount_path == "/mnt/session/repo", "the repo mount path must be recorded"
    assert snapshot.memory_store_id == "memstore_7", "the mounted memory store must be recorded"
    assert snapshot.vault_id == "vault_9", "an unspecified vault falls back to the session's own"
    assert snapshot.environment_id == "env_science", "the environment is frozen at create time"
    assert snapshot.agent_version == 3, "the agent version is kept for diagnostics"
    assert snapshot.agent_name == "research-bot", "the agent name is kept for diagnostics"


def test_snapshot_survives_a_json_round_trip() -> None:
    snapshot = desired_snapshot(
        _agent(),
        hidden_mcp_server_names=frozenset(),
        environment_id="env_science",
        env_sha256="env-hash",
        repo_url="https://github.com/pymc-labs/example",
        repo_branch="main",
        memory_store_id="memstore_7",
        vault_id="vault_9",
        env_file_id="file_env",
        repo_mount_path="/mnt/session/repo",
        repo_token_issued_at=1789000000,
    )

    restored = SessionSnapshot.model_validate_json(snapshot.model_dump_json())

    assert restored == snapshot, "a snapshot must survive the JSONB round trip unchanged"
    assert restored.schema_version == 1, "the schema version must round-trip for future migrations"
    assert fingerprint_identity(restored) == fingerprint_identity(snapshot), (
        "fingerprints computed after a round trip must match, or reload looks like a change"
    )


def test_identity_fingerprint_ignores_handles_and_diagnostics() -> None:
    base = desired_snapshot(
        _agent(),
        hidden_mcp_server_names=frozenset(),
        environment_id="env_science",
        env_sha256="env-hash",
        repo_url="https://github.com/pymc-labs/example",
        repo_branch="main",
        memory_store_id="memstore_7",
        vault_id="vault_9",
    )
    rehandled = base.model_copy(
        update={
            "env_file_id": "file_rotated",
            "env_resource_id": "res_rotated",
            "repo_resource_id": "res_repo_2",
            "repo_token_issued_at": 1789999999,
            "agent_version": 99,
            "agent_name": "research-bot-renamed",
        }
    )

    assert fingerprint_identity(rehandled) == fingerprint_identity(base), (
        "rotating a handle or bumping the agent version is not a configuration change"
    )
    assert fingerprint_mutable(rehandled) == fingerprint_mutable(base), (
        "handles are excluded from the mutable axis too"
    )


def test_a_model_change_moves_only_the_identity_fingerprint() -> None:
    haiku = desired_snapshot(
        _agent(model_id="claude-haiku-4-5"),
        hidden_mcp_server_names=frozenset(),
        environment_id="env_science",
        env_sha256="env-hash",
        repo_url=None,
        repo_branch=None,
        memory_store_id=None,
        vault_id=None,
    )
    sonnet = desired_snapshot(
        _agent(model_id="claude-sonnet-5"),
        hidden_mcp_server_names=frozenset(),
        environment_id="env_science",
        env_sha256="env-hash",
        repo_url=None,
        repo_branch=None,
        memory_store_id=None,
        vault_id=None,
    )

    assert fingerprint_identity(haiku) != fingerprint_identity(sonnet), (
        "a model change can only be applied by replacing the session"
    )
    assert fingerprint_mutable(haiku) == fingerprint_mutable(sonnet), (
        "a model change must not masquerade as an in-place refresh"
    )


def test_an_env_change_moves_only_the_mutable_fingerprint() -> None:
    before = desired_snapshot(
        _agent(),
        hidden_mcp_server_names=frozenset(),
        environment_id="env_science",
        env_sha256="hash-before",
        repo_url=None,
        repo_branch=None,
        memory_store_id=None,
        vault_id=None,
    )
    after = desired_snapshot(
        _agent(),
        hidden_mcp_server_names=frozenset(),
        environment_id="env_science",
        env_sha256="hash-after",
        repo_url=None,
        repo_branch=None,
        memory_store_id=None,
        vault_id=None,
    )

    assert fingerprint_mutable(before) != fingerprint_mutable(after), (
        "a new key must read as a mutable change so the live session gets it"
    )
    assert fingerprint_identity(before) == fingerprint_identity(after), (
        "a key change must never force a replacement"
    )


def test_a_hidden_server_is_not_in_the_desired_mutable_fingerprint() -> None:
    """The no-churn property: a session created without a personally-connected
    server must read as up to date on the next turn. Hash the agent's own list
    and every turn would diff, push the server back on, and hand the caller a
    server they cannot authenticate."""
    personal = BetaManagedAgentsMCPServerURLDefinition(
        name="docs", type="url", url="https://mcp.example.com/docs"
    )
    daimon = BetaManagedAgentsMCPServerURLDefinition(
        name="daimon-mcp", type="url", url="https://mcp.example/daimon"
    )
    agent = BetaManagedAgentsAgent(
        id="agent_research",
        archived_at=None,
        created_at=_NOW,
        description=None,
        mcp_servers=[personal, daimon],
        metadata={},
        model=BetaManagedAgentsModelConfig(id="claude-sonnet-5"),
        name="research-bot",
        skills=[],
        system="be useful",
        tools=[],
        type="agent",
        updated_at=_NOW,
        version=1,
    )

    filtered = desired_snapshot(
        agent,
        hidden_mcp_server_names=frozenset({"docs"}),
        environment_id="env_science",
        env_sha256=None,
        repo_url=None,
        repo_branch=None,
        memory_store_id=None,
        vault_id=None,
    )

    assert filtered.mcp_servers_sha256 == hash_mcp_servers([daimon]), (
        "the desired snapshot describes the session this caller would get"
    )
    assert filtered.mcp_servers_sha256 != hash_mcp_servers([personal, daimon]), (
        "hashing the agent's own list is exactly the drift loop this prevents"
    )


def test_read_only_policy_without_a_memory_store_does_not_create_phantom_drift() -> None:
    desired = desired_snapshot(
        _agent(),
        hidden_mcp_server_names=frozenset(),
        environment_id="env_science",
        env_sha256=None,
        repo_url=None,
        repo_branch=None,
        memory_store_id=None,
        memory_read_only=True,
        vault_id=None,
    )
    absent_mount = desired.model_copy(update={"memory_read_only": False})
    assert fingerprint_identity(desired) == fingerprint_identity(absent_mount)


def _skilled_agent(*skills: BetaManagedAgentsCustomSkill) -> BetaManagedAgentsAgent:
    return _agent().model_copy(update={"skills": list(skills)})


def _created_echoing(
    agent: BetaManagedAgentsAgent, *echoed: BetaManagedAgentsCustomSkill
) -> BetaManagedAgentsSession:
    """A just-created session whose agent reports `echoed`, the versions MA resolved."""
    return BetaManagedAgentsSession(
        id="sess_pinned",
        agent=BetaManagedAgentsSessionAgent(
            id=agent.id,
            description=None,
            mcp_servers=[],
            model=agent.model,
            name=agent.name,
            skills=list(echoed),
            system=agent.system,
            tools=[],
            type="agent",
            version=agent.version,
        ),
        archived_at=None,
        created_at=_NOW,
        environment_id="env_science",
        metadata={},
        outcome_evaluations=[],
        resources=[],
        stats=BetaManagedAgentsSessionStats(),
        status="idle",
        title=None,
        type="session",
        updated_at=_NOW,
        usage=BetaManagedAgentsSessionUsage(),
        vault_ids=[],
    )


def _desired_for(agent: BetaManagedAgentsAgent) -> SessionSnapshot:
    return desired_snapshot(
        agent,
        hidden_mcp_server_names=frozenset(),
        environment_id="env_science",
        env_sha256=None,
        repo_url=None,
        repo_branch=None,
        memory_store_id=None,
        vault_id=None,
    )


def test_a_latest_skill_echoed_back_pinned_is_not_skills_drift() -> None:
    latest = BetaManagedAgentsCustomSkill(skill_id="skill_style", type="custom", version="latest")
    pinned = BetaManagedAgentsCustomSkill(
        skill_id="skill_style", type="custom", version="1791535473768193"
    )
    agent = _skilled_agent(latest)
    recorded = snapshot_from_created_session(
        _created_echoing(agent, pinned),
        env_sha256=None,
        env_file_id=None,
        repo_token_issued_at=None,
        vault_id=None,
        sent_skills=session_skills(agent),
    )

    assert identity_change_reasons(recorded, _desired_for(agent)) == (), (
        "MA resolving 'latest' to a version must not replace the session on every turn"
    )


def test_a_real_skill_change_after_creation_is_still_skills_drift() -> None:
    style = BetaManagedAgentsCustomSkill(skill_id="skill_style", type="custom", version="latest")
    charts = BetaManagedAgentsCustomSkill(skill_id="skill_charts", type="custom", version="latest")
    created_with = _skilled_agent(style)
    recorded = snapshot_from_created_session(
        _created_echoing(created_with, style.model_copy(update={"version": "17"})),
        env_sha256=None,
        env_file_id=None,
        repo_token_issued_at=None,
        vault_id=None,
        sent_skills=session_skills(created_with),
    )

    assert identity_change_reasons(recorded, _desired_for(_skilled_agent(style, charts))) == (
        "skills",
    ), "adding a skill to the agent must still replace the session"
