"""DB-backed behavior tests for _create_agent_impl auto-sync.

Tests in this file require a real Postgres via db_session_factory — they exercise
sync_agent_skills which does real DB upserts. Do NOT put these
in test_agents.py (which uses MagicMock() for session_factory and is pure-unit).

Patterns followed:
- Transport-level SDK fakes via MARouter + build_fake_anthropic (no AsyncMock on client.beta.*)
- SDK response objects constructed inline via real constructors (no model_construct)
- GitHub tarball fetch mocked at the GitHubTarballFetcher class boundary
  (the tool builds its own httpx.AsyncClient inline, so class-level patching is
  the correct approach here — not a T1/T2/T3 violation)
"""

from __future__ import annotations

import re
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools.agents import (
    _create_agent_impl,  # pyright: ignore[reportPrivateUsage]
)
from daimon.core.scope import DeploymentDefault
from daimon.core.specs import SkillRepo
from daimon.core.stores.domain import Role
from daimon.testing import make_fernet
from daimon.testing.factories import make_cli_principal
from daimon.testing.ma_models import ma_agent
from mux.testing.drivers.anthropic.ma import MARouter, build_fake_anthropic, list_response
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Test: auto-sync on create
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Test: sync_warnings populated on partial failure
# ---------------------------------------------------------------------------


async def test_create_agent_impl_returns_sync_warnings_on_partial_failure(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """When sync fails for a repo, create_agent still returns AgentInfo (no raise).

    Verifies that agents.create succeeded → tool NEVER
    raises even when every repo sync fails. The returned AgentInfo.sync_warnings
    carries the failure details so the LLM caller can surface them.
    """
    cli = await make_cli_principal(db_session, os_user="bob")
    await db_session.commit()
    fernet = make_fernet()
    account_id = cli.account_id
    tenant_id = cli.tenant_id
    agent_name = "failing-sync-agent"
    agent_id = "ag_created_2"

    # GitHubTarballFetcher.fetch_tarball will raise to simulate a fetch failure.

    def on_agents_create(_req: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        return httpx.Response(
            200,
            json=ma_agent(
                id=agent_id,
                name=agent_name,
                model="claude-opus-4-5",
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
        # Call 3+: skill_sync lookup — return created agent so sync resolves it.
        if agents_list_call_count_2 <= 2:
            return list_response([])
        return list_response(
            [
                ma_agent(
                    id=agent_id,
                    name=agent_name,
                    model="claude-opus-4-5",
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
            json=ma_agent(
                id=m.group(1),
                name=agent_name,
                model="claude-opus-4-5",
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

    anthropic_client = build_fake_anthropic(router.dispatch)

    settings = MagicMock()  # type: ignore[var-annotated]
    settings.mcp.public_url = None  # no daimon-mcp merge needed in sync tests
    settings.github.app_id = None  # no App configured in these sync tests
    settings.github.app_private_key = None
    settings.github.fallback_pat = None
    settings.github.max_tarball_decompressed_bytes = 200 * 1024 * 1024
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
        # tool must NOT raise even when sync fails entirely
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
