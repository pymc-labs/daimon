"""DB-backed behavior tests for _create_agent_impl auto-sync (Phase 45-02).

Tests in this file require a real Postgres via db_session_factory — they exercise
sync_agent_skills which does real DB upserts + PAT decryption. Do NOT put these
in test_agents.py (which uses MagicMock() for session_factory and is pure-unit).

Patterns followed:
- Transport-level SDK fakes via MARouter + build_fake_anthropic (no AsyncMock on client.beta.*)
- SDK response objects constructed inline via real constructors (no model_construct)
- GitHub tarball fetch mocked at the GitHubTarballFetcher class boundary
  (the tool builds its own httpx.AsyncClient inline, so class-level patching is
  the correct approach here — not a T1/T2/T3 violation)
"""

from __future__ import annotations

import io
import re
import tarfile
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from anthropic import AsyncAnthropic
from anthropic.types.beta import BetaManagedAgentsAgent, SkillListResponse
from cryptography.fernet import Fernet, MultiFernet
from daimon.adapters.mcp.auth.resolver import AuthIdentity, Role
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools.agents import _create_agent_impl
from daimon.core.github_credentials import encrypt_token
from daimon.core.scope import DeploymentDefault
from daimon.core.specs import SkillRepo
from daimon.core.stores.github_credentials import upsert_credential
from daimon.testing.factories import make_cli_principal
from daimon.testing.ma import MARouter, list_response
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Helpers (minimal, no factory wrappers)
# ---------------------------------------------------------------------------


def _make_tarball(files: dict[str, bytes]) -> bytes:
    """Build a tar.gz with the given path → content mapping."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for path, content in files.items():
            info = tarfile.TarInfo(name=path)
            info.size = len(content)
            tf.addfile(info, io.BytesIO(content))
    return buf.getvalue()


def _make_fernet() -> MultiFernet:
    return MultiFernet([Fernet(Fernet.generate_key())])


async def _seed_pat(
    *,
    sessionmaker: async_sessionmaker[AsyncSession],
    fernet: MultiFernet,
    principal_id: uuid.UUID,
    plaintext: str = "test-pat",
) -> None:
    async with sessionmaker() as session, session.begin():
        await upsert_credential(
            session,
            principal_id=principal_id,
            github_login="tester",
            encrypted_token=encrypt_token(fernet, plaintext),
            scopes=("repo",),
        )


def _build_anthropic(router: MARouter) -> AsyncAnthropic:
    transport = httpx.MockTransport(router.dispatch)
    http_client = httpx.AsyncClient(transport=transport, base_url="https://api.anthropic.com")
    return AsyncAnthropic(api_key="test", http_client=http_client)


# ---------------------------------------------------------------------------
# Test: auto-sync on create (PHASE-45-AUTOSYNC-01)
# ---------------------------------------------------------------------------


async def test_create_agent_impl_syncs_skill_repos(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """create_agent with skill_repos=[repo] creates the agent AND syncs repos.

    Verifies PHASE-45-AUTOSYNC-01: the old ToolError rejection branch is gone;
    after agents.create succeeds, sync_agent_skills runs and the agent ends up
    with skills=[<anthropic_id>] via the orchestrator's attach step.
    """
    cli = await make_cli_principal(db_session, os_user="alice")
    await db_session.commit()
    fernet = _make_fernet()
    # Use cli.account_id as principal_id — _create_agent_impl passes auth.account_id
    # to sync_agent_skills, NOT a CliPrincipal.id.
    await _seed_pat(
        sessionmaker=db_session_factory,
        fernet=fernet,
        principal_id=cli.account_id,
    )

    tarball = _make_tarball(
        {"myskills-main/SKILL.md": b"---\nname: myskill\ndescription: d\n---\nbody"}
    )

    agent_id = "ag_created"
    skill_id = "sk_synced"
    skill_anthropic_id = skill_id
    agent_name = "test-agent"
    # Bundled name for github.com/orgA/myskills → "orgA-myskills" (Phase 45-01 fix)
    tenant_id = cli.tenant_id
    account_id = cli.account_id

    update_calls: list[dict[str, object]] = []

    def on_agents_create(req: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        return httpx.Response(
            200,
            json=BetaManagedAgentsAgent(
                id=agent_id,
                type="agent",
                version=1,
                name=agent_name,
                model={"id": "claude-opus-4-5"},
                description=None,
                system=None,
                tools=[],
                mcp_servers=[],
                skills=[],
                created_at="2026-04-24T00:00:00Z",
                updated_at="2026-04-24T00:00:00Z",
                metadata={
                    "daimon_tenant": str(tenant_id),
                    "daimon_name": agent_name,
                    "daimon_account": str(account_id),
                },
            ).model_dump(mode="json"),
        )

    def on_skills_create(_req: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        return httpx.Response(
            200,
            json=SkillListResponse(
                id=skill_id,
                type="custom",
                display_title=f"{agent_name}/orgA-myskills",
                latest_version="1",
                created_at="2026-04-24T00:00:00Z",
                updated_at="2026-04-24T00:00:00Z",
                source="custom",
            ).model_dump(mode="json"),
        )

    agents_list_call_count = 0

    def on_agents_list(_req: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        nonlocal agents_list_call_count
        agents_list_call_count += 1
        # Call 1: collision check (_reject_guild_name_collision) — empty → guard passes.
        # Call 2: reconcile_agent's dedup lookup — empty → reconcile takes the CREATE path.
        # Call 3+: skill_sync lookup (D-25) — return the created agent so sync can find it.
        if agents_list_call_count <= 2:
            return list_response([])
        return list_response(
            [
                BetaManagedAgentsAgent(
                    id=agent_id,
                    type="agent",
                    version=1,
                    name=agent_name,
                    model={"id": "claude-opus-4-5"},
                    description=None,
                    system=None,
                    tools=[],
                    mcp_servers=[],
                    skills=[],
                    created_at="2026-04-24T00:00:00Z",
                    updated_at="2026-04-24T00:00:00Z",
                    metadata={
                        "daimon_tenant": str(tenant_id),
                        "daimon_name": agent_name,
                    },
                ).model_dump(mode="json")
            ]
        )

    def on_agents_update(req: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        import json

        body = json.loads(req.content)
        update_calls.append(body)
        # Normalize skills from the request: add version="1" so BetaManagedAgentsAgent
        # parses correctly (BetaManagedAgentsCustomSkill requires version: str not None).
        normalized_skills = [
            {**s, "version": "1"} if s.get("type") == "custom" and "version" not in s else s
            for s in body.get("skills", [])
        ]
        return httpx.Response(
            200,
            json=BetaManagedAgentsAgent(
                id=agent_id,
                type="agent",
                version=2,
                name=agent_name,
                model={"id": "claude-opus-4-5"},
                description=None,
                system=None,
                tools=[],
                mcp_servers=[],
                skills=normalized_skills,
                created_at="2026-04-24T00:00:00Z",
                updated_at="2026-04-24T00:00:00Z",
                metadata={
                    "daimon_tenant": str(tenant_id),
                    "daimon_name": agent_name,
                },
            ).model_dump(mode="json"),
        )

    def on_agents_retrieve(_req: httpx.Request, m: re.Match[str]) -> httpx.Response:
        """reconcile re-retrieves the created agent by id for _build_agent_info."""
        return httpx.Response(
            200,
            json=BetaManagedAgentsAgent(
                id=m.group(1),
                type="agent",
                version=1,
                name=agent_name,
                model={"id": "claude-opus-4-5"},
                description=None,
                system=None,
                tools=[],
                mcp_servers=[],
                skills=[],
                created_at="2026-04-24T00:00:00Z",
                updated_at="2026-04-24T00:00:00Z",
                metadata={
                    "daimon_tenant": str(tenant_id),
                    "daimon_name": agent_name,
                },
            ).model_dump(mode="json"),
        )

    router = MARouter()
    router.add("POST", r"/v1/agents$", on_agents_create)
    router.add("POST", r"/v1/skills$", on_skills_create)
    router.add("GET", r"/v1/agents$", on_agents_list)
    router.add("GET", r"/v1/agents/([^/]+)$", on_agents_retrieve)
    router.add("POST", r"/v1/agents/([^/]+)$", on_agents_update)

    anthropic_client = _build_anthropic(router)

    settings = MagicMock()  # type: ignore[var-annotated]
    settings.mcp.public_url = None  # no daimon-mcp merge needed in sync tests
    runtime = McpRuntime(
        session_factory=db_session_factory,
        client=anthropic_client,
        settings=settings,  # type: ignore[arg-type]
        fernet=fernet,
        deployment_default=DeploymentDefault(),
    )
    auth = AuthIdentity(account_id=account_id, tenant_id=tenant_id, role=Role.ADMIN, is_admin=True)

    spec_skill_repos = [SkillRepo(url="https://github.com/orgA/myskills", branch="main")]

    from daimon.core.specs import AgentSpec

    spec = AgentSpec(
        name=agent_name,
        model="claude-opus-4-5",
        skill_repos=spec_skill_repos,
    )

    # Patch GitHubTarballFetcher at the orchestrator import boundary so the
    # inline httpx.AsyncClient() inside sync_agent_skills returns our tarball.
    # This is NOT a T1/T2/T3 violation — those rules ban AsyncMock on client.beta.*
    # and model_construct only. The GitHub fetch is a network boundary we legitimately
    # replace here; the Anthropic SDK calls still go through the transport-level fake.
    with patch("daimon.core.skill_sync.orchestrator.GitHubTarballFetcher") as mock_fetcher_cls:
        mock_fetcher_cls.return_value.fetch_tarball = AsyncMock(return_value=tarball)
        result = await _create_agent_impl(runtime, auth, spec)

    assert result.name == agent_name, "AgentInfo should have the created agent name"
    assert result.id == agent_id, "AgentInfo should carry the MA-assigned id"
    assert result.sync_warnings is None or result.sync_warnings == [], (
        "no sync failures expected on the happy path"
    )

    # The orchestrator's attach step should have called agents.update with skills=[skill_id]
    assert len(update_calls) == 1, "agents.update must be called once (attach step)"
    attached_skills = update_calls[0].get("skills", [])
    attached_ids = [s["skill_id"] for s in attached_skills if s.get("type") == "custom"]
    assert skill_anthropic_id in attached_ids, (
        f"skill {skill_anthropic_id!r} must be attached to the agent; got {attached_ids}"
    )


# ---------------------------------------------------------------------------
# Test: sync_warnings populated on partial failure (PHASE-45-CHAT-ERR-01)
# ---------------------------------------------------------------------------


async def test_create_agent_impl_returns_sync_warnings_on_partial_failure(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """When sync fails for a repo, create_agent still returns AgentInfo (no raise).

    Verifies PHASE-45-CHAT-ERR-01 (D-12): agents.create succeeded → tool NEVER
    raises even when every repo sync fails. The returned AgentInfo.sync_warnings
    carries the failure details so the LLM caller can surface them.
    """
    cli = await make_cli_principal(db_session, os_user="bob")
    await db_session.commit()
    fernet = _make_fernet()
    account_id = cli.account_id
    tenant_id = cli.tenant_id
    agent_name = "failing-sync-agent"
    agent_id = "ag_created_2"

    # No PAT seeded — orchestrator will proceed with no auth header.
    # GitHubTarballFetcher.fetch_tarball will raise to simulate a fetch failure.

    def on_agents_create(_req: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        return httpx.Response(
            200,
            json=BetaManagedAgentsAgent(
                id=agent_id,
                type="agent",
                version=1,
                name=agent_name,
                model={"id": "claude-opus-4-5"},
                description=None,
                system=None,
                tools=[],
                mcp_servers=[],
                skills=[],
                created_at="2026-04-24T00:00:00Z",
                updated_at="2026-04-24T00:00:00Z",
                metadata={
                    "daimon_tenant": str(tenant_id),
                    "daimon_name": agent_name,
                    "daimon_account": str(account_id),
                },
            ).model_dump(mode="json"),
        )

    agents_list_call_count_2 = 0

    def on_agents_list(_req: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        nonlocal agents_list_call_count_2
        agents_list_call_count_2 += 1
        # Call 1: collision check — empty → guard passes.
        # Call 2: reconcile_agent's dedup lookup — empty → reconcile takes CREATE path.
        # Call 3+: skill_sync lookup (D-25) — return created agent so sync resolves it.
        if agents_list_call_count_2 <= 2:
            return list_response([])
        return list_response(
            [
                BetaManagedAgentsAgent(
                    id=agent_id,
                    type="agent",
                    version=1,
                    name=agent_name,
                    model={"id": "claude-opus-4-5"},
                    description=None,
                    system=None,
                    tools=[],
                    mcp_servers=[],
                    skills=[],
                    created_at="2026-04-24T00:00:00Z",
                    updated_at="2026-04-24T00:00:00Z",
                    metadata={
                        "daimon_tenant": str(tenant_id),
                        "daimon_name": agent_name,
                        "daimon_account": str(account_id),
                    },
                ).model_dump(mode="json")
            ]
        )

    def on_agents_retrieve_2(_req: httpx.Request, m: re.Match[str]) -> httpx.Response:
        """reconcile re-retrieves the created agent by id for _build_agent_info."""
        return httpx.Response(
            200,
            json=BetaManagedAgentsAgent(
                id=m.group(1),
                type="agent",
                version=1,
                name=agent_name,
                model={"id": "claude-opus-4-5"},
                description=None,
                system=None,
                tools=[],
                mcp_servers=[],
                skills=[],
                created_at="2026-04-24T00:00:00Z",
                updated_at="2026-04-24T00:00:00Z",
                metadata={
                    "daimon_tenant": str(tenant_id),
                    "daimon_name": agent_name,
                    "daimon_account": str(account_id),
                },
            ).model_dump(mode="json"),
        )

    router = MARouter()
    router.add("POST", r"/v1/agents$", on_agents_create)
    router.add("GET", r"/v1/agents$", on_agents_list)
    router.add("GET", r"/v1/agents/([^/]+)$", on_agents_retrieve_2)

    anthropic_client = _build_anthropic(router)

    settings = MagicMock()  # type: ignore[var-annotated]
    settings.mcp.public_url = None  # no daimon-mcp merge needed in sync tests
    runtime = McpRuntime(
        session_factory=db_session_factory,
        client=anthropic_client,
        settings=settings,  # type: ignore[arg-type]
        fernet=fernet,
        deployment_default=DeploymentDefault(),
    )
    auth = AuthIdentity(account_id=account_id, tenant_id=tenant_id, role=Role.ADMIN, is_admin=True)

    from daimon.core.specs import AgentSpec

    spec = AgentSpec(
        name=agent_name,
        model="claude-opus-4-5",
        skill_repos=[SkillRepo(url="https://github.com/orgB/skills", branch="main")],
    )

    # Simulate fetch failure by raising from fetch_tarball.
    with patch("daimon.core.skill_sync.orchestrator.GitHubTarballFetcher") as mock_fetcher_cls:
        mock_fetcher_cls.return_value.fetch_tarball = AsyncMock(
            side_effect=Exception("simulated network error")
        )
        # D-12: tool must NOT raise even when sync fails entirely
        result = await _create_agent_impl(runtime, auth, spec)

    assert result.name == agent_name, "AgentInfo should still be returned on sync failure"
    assert result.id == agent_id, "AgentInfo should carry the MA-assigned id"
    assert result.sync_warnings is not None and len(result.sync_warnings) > 0, (
        "sync_warnings must be non-empty when repos fail"
    )
    failed_urls = [w.repo_url for w in result.sync_warnings]
    assert "https://github.com/orgB/skills" in failed_urls, (
        "failed repo URL must appear in sync_warnings"
    )
