"""What a live MA session is running, reduced to two comparable fingerprints.

Every SDK object here is constructed inline with the real constructor: when the
SDK adds a required field these break, which is the point — a snapshot that
silently stopped covering a field would read as "no configuration change" and
keep a caller on a stale session forever.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Literal

import pytest
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
from daimon.core.session_compat import identity_change_reasons, valid_unavailable_repo_urls
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
    refreshed_unavailable_state,
    session_skills,
    snapshot_from_created_session,
    snapshot_from_retrieved_session,
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
                url="https://github.com/example/widgets",
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
    assert snapshot.repo_url is None, "App sessions record repositories as a set"
    assert snapshot.repo_urls == ("https://github.com/example/widgets",)
    assert snapshot.repo_branch is None, "App sessions do not pin a legacy branch"
    assert snapshot.repo_mount_path == "/mnt/session/repo", "the repo mount path must be recorded"
    assert snapshot.memory_store_id == "memstore_7", "the mounted memory store must be recorded"
    assert snapshot.vault_id == "vault_9", "an unspecified vault falls back to the session's own"
    assert snapshot.environment_id == "env_science", "the environment is frozen at create time"
    assert snapshot.agent_version == 3, "the agent version is kept for diagnostics"
    assert snapshot.agent_name == "research-bot", "the agent name is kept for diagnostics"
    backfilled = snapshot_from_retrieved_session(session)
    assert backfilled.github_mode == "app"
    assert "github_mode" not in identity_change_reasons(backfilled, snapshot)


def test_snapshot_survives_a_json_round_trip() -> None:
    snapshot = desired_snapshot(
        _agent(),
        hidden_mcp_server_names=frozenset(),
        environment_id="env_science",
        env_sha256="env-hash",
        repo_url="https://github.com/example/widgets",
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
        repo_url="https://github.com/example/widgets",
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


def _session_with_memory(*mounts: BetaManagedAgentsMemoryStoreResource) -> BetaManagedAgentsSession:
    return BetaManagedAgentsSession(
        id="sess_mem",
        agent=BetaManagedAgentsSessionAgent(
            id="agent_research",
            description=None,
            mcp_servers=[],
            model=BetaManagedAgentsModelConfig(id="claude-sonnet-5"),
            name="research-bot",
            skills=[],
            system="be useful",
            tools=[],
            type="agent",
            version=3,
        ),
        archived_at=None,
        created_at=_NOW,
        environment_id="env_science",
        metadata={},
        outcome_evaluations=[],
        resources=list(mounts),
        stats=BetaManagedAgentsSessionStats(),
        status="idle",
        title=None,
        type="session",
        updated_at=_NOW,
        usage=BetaManagedAgentsSessionUsage(),
        vault_ids=[],
    )


def test_the_personal_store_is_told_apart_by_id_whatever_the_mount_order() -> None:
    personal = BetaManagedAgentsMemoryStoreResource(
        memory_store_id="memstore_alice", type="memory_store", access="read_write"
    )
    shared = BetaManagedAgentsMemoryStoreResource(
        memory_store_id="memstore_shared", type="memory_store", access="read_only"
    )

    for order in ((shared, personal), (personal, shared)):
        snapshot = snapshot_from_created_session(
            _session_with_memory(*order),
            env_sha256=None,
            env_file_id=None,
            repo_token_issued_at=None,
            vault_id=None,
            sent_skills=[],
            personal_memory_store_ids=("memstore_alice",),
        )
        assert snapshot.memory_store_id == "memstore_shared"
        assert snapshot.personal_memory_store_id == "memstore_alice"
        assert not snapshot.memory_read_only, "one writable mount is writable memory"


@pytest.mark.parametrize(
    ("shared", "personal", "read_only"),
    [
        (None, None, False),
        ("read_only", None, True),
        ("read_write", None, False),
        (None, "read_only", True),
        (None, "read_write", False),
        ("read_only", "read_only", True),
        ("read_write", "read_write", False),
    ],
    ids=[
        "neither",
        "shared-ro",
        "shared-rw",
        "personal-ro",
        "personal-rw",
        "both-ro",
        "both-rw",
    ],
)
def test_memory_access_covers_every_mounted_store(
    shared: Literal["read_only", "read_write"] | None,
    personal: Literal["read_only", "read_write"] | None,
    read_only: bool,
) -> None:
    """A session holding only a personal mount (the shared store failed to
    provision) still has memory a read-only turn must not write."""
    mounts = [
        BetaManagedAgentsMemoryStoreResource(
            memory_store_id=store, type="memory_store", access=access
        )
        for store, access in (("memstore_shared", shared), ("memstore_alice", personal))
        if access is not None
    ]
    observed = snapshot_from_created_session(
        _session_with_memory(*mounts),
        env_sha256=None,
        env_file_id=None,
        repo_token_issued_at=None,
        vault_id=None,
        sent_skills=[],
        personal_memory_store_ids=("memstore_alice",),
    )
    desired_read_only = desired_snapshot(
        _agent(),
        hidden_mcp_server_names=frozenset(),
        environment_id="env_science",
        env_sha256=None,
        repo_url=None,
        repo_branch=None,
        memory_store_id="memstore_shared" if shared else None,
        personal_memory_store_id="memstore_alice" if personal else None,
        memory_read_only=True,
        vault_id=None,
    )

    assert observed.memory_read_only is read_only
    assert desired_read_only.memory_read_only is (shared is not None or personal is not None), (
        "a read-only turn wants read-only memory wherever any store is mounted"
    )
    if shared is not None or personal is not None:
        assert ("memory_access" in identity_change_reasons(observed, desired_read_only)) is (
            not read_only
        ), "a writable mount of either store is replaced for a read-only turn"


def test_a_snapshot_without_a_personal_store_keeps_its_recorded_fingerprint() -> None:
    """Rows recorded before personal memory have no such field. Their identity
    fingerprint must not move, or every live row would look changed."""
    snapshot = desired_snapshot(
        _agent(),
        hidden_mcp_server_names=frozenset(),
        environment_id="env_science",
        env_sha256=None,
        repo_url=None,
        repo_branch=None,
        memory_store_id="memstore_7",
        vault_id=None,
    )
    legacy = SessionSnapshot.model_validate(
        snapshot.model_dump(exclude={"personal_memory_store_id"})
    )
    with_personal = snapshot.model_copy(update={"personal_memory_store_id": "memstore_alice"})

    assert fingerprint_identity(legacy) == fingerprint_identity(snapshot)
    assert fingerprint_identity(with_personal) != fingerprint_identity(snapshot)


def test_a_members_snapshot_keeps_its_recorded_fingerprint_and_json() -> None:
    """Rows recorded before the external flag existed must not look changed:
    only a session made for someone from another organisation records it."""
    snapshot = desired_snapshot(
        _agent(),
        hidden_mcp_server_names=frozenset(),
        environment_id="env_science",
        env_sha256=None,
        repo_url=None,
        repo_branch=None,
        memory_store_id=None,
        vault_id=None,
    )
    external = snapshot.model_copy(update={"is_external": True})

    assert "is_external" not in snapshot.model_dump_json(), "a member's row omits the flag"
    assert SessionSnapshot.model_validate_json(external.model_dump_json()) == external, (
        "an external caller's flag survives the JSONB round trip"
    )
    assert fingerprint_identity(external) != fingerprint_identity(snapshot), (
        "the flag is session identity: flipping it replaces the session"
    )


def test_old_snapshot_unavailability_fields_are_ignored() -> None:
    snapshot = desired_snapshot(
        _agent(),
        hidden_mcp_server_names=frozenset(),
        environment_id="env_science",
        env_sha256=None,
        repo_url=None,
        repo_branch=None,
        memory_store_id=None,
        vault_id=None,
    )
    legacy = SessionSnapshot.model_validate(
        {
            **snapshot.model_dump(exclude={"unavailable_repos"}),
            "unavailable_repo_urls": ("https://github.com/acme/refused",),
            "unavailable_since": _NOW,
            "unavailable_notice_announced": False,
        }
    )
    assert legacy.unavailable_repos == ()
    assert legacy.unavailable_notice_pending == ()
    assert valid_unavailable_repo_urls(legacy, now=_NOW) == ()


def test_refusal_keeps_first_time_and_announces_once_until_access_returns() -> None:
    url = "https://github.com/acme/refused"
    snapshot = desired_snapshot(
        _agent(),
        hidden_mcp_server_names=frozenset(),
        environment_id="env_science",
        env_sha256=None,
        repo_url=None,
        repo_branch=None,
        memory_store_id=None,
        vault_id=None,
    )
    first = snapshot.model_copy(update=refreshed_unavailable_state(snapshot, (url,), now=_NOW))
    assert first.unavailable_repos == ((url, _NOW),)
    assert first.unavailable_notice_pending == (url,)
    dropped_before_notice = first.model_copy(
        update=refreshed_unavailable_state(first, (), now=_NOW + timedelta(minutes=61))
    )
    assert dropped_before_notice.unavailable_repos == ()
    assert dropped_before_notice.unavailable_notice_pending == (url,)
    announced = first.model_copy(
        update={
            "unavailable_notice_pending": (),
            "unavailable_notice_sent": (url,),
        }
    )
    later = _NOW + timedelta(minutes=40)
    refused_again = announced.model_copy(
        update=refreshed_unavailable_state(announced, (url,), now=later)
    )
    assert refused_again.unavailable_repos == ((url, _NOW),)
    assert refused_again.unavailable_notice_pending == ()
    dropped = refused_again.model_copy(
        update=refreshed_unavailable_state(refused_again, (), now=later)
    )
    assert dropped.unavailable_repos == ()
    assert dropped.unavailable_notice_sent == ()
    anew = dropped.model_copy(update=refreshed_unavailable_state(dropped, (url,), now=later))
    assert anew.unavailable_repos == ((url, later),)
    assert anew.unavailable_notice_pending == (url,)


def test_native_fast_model_is_in_session_identity_and_pricing():
    agent = _agent(model_id="claude-opus-5-5")
    kwargs = dict(
        hidden_mcp_server_names=frozenset(),
        environment_id="env",
        env_sha256=None,
        repo_url=None,
        repo_branch=None,
        memory_store_id=None,
        vault_id=None,
    )
    standard = desired_snapshot(agent, **kwargs)
    agent = agent.model_copy(
        update={"model": BetaManagedAgentsModelConfig(id="claude-opus-5-5", speed="fast")}
    )
    fast = desired_snapshot(agent, **kwargs)
    assert fast.model_id == "claude-opus-5-5"
    assert fast.pricing_id == "claude-opus-5-5-fast"
    assert identity_change_reasons(standard, fast) == ("model",)
    assert fingerprint_identity(fast) != fingerprint_identity(standard)
