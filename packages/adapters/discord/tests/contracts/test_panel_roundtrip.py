"""Live-MA contract tests for the /agent-setup panel write paths.

Gated by DAIMON_TEST_ANTHROPIC_API_KEY (see conftest). Per-test cleanup via
RUN_TAG-scoped names; no global delete_entire_workspace_for_testing.
"""

from __future__ import annotations

import uuid

import pytest
from anthropic import AsyncAnthropic
from daimon.core.defaults.reconcile_agents import reconcile_agent
from daimon.core.specs import AgentSpec

pytestmark = pytest.mark.contract


RUN_TAG = uuid.uuid4().hex[:8]
TENANT_ID = uuid.uuid4()
ACCOUNT_ID = uuid.uuid4()


async def _cleanup(client: AsyncAnthropic, *names: str) -> None:
    """Best-effort: archive any agent whose name matches one of ``names``."""
    try:
        agents = await client.beta.agents.list()  # pyright: ignore[reportUnknownMemberType]
        for a in agents.data:
            if a.name in names:
                try:
                    await client.beta.agents.archive(a.id)  # pyright: ignore[reportUnknownMemberType]
                except Exception as e:  # noqa: BLE001 - best-effort cleanup
                    print(f"    cleanup: failed to archive {a.id}: {e}")
    except Exception as e:  # noqa: BLE001 - best-effort cleanup
        print(f"    cleanup: list failed: {e}")


@pytest.mark.asyncio
async def test_new_agent_metadata_roundtrips_to_ma(anthropic_client: AsyncAnthropic) -> None:
    """`new` path: reconcile_agent on a blank spec; assert metadata round-trips."""
    name = f"phase35-new-{RUN_TAG}"
    spec = AgentSpec.model_validate(
        {"name": name, "model": "claude-sonnet-4-6", "system": "phase 35 new"}
    )
    try:
        await reconcile_agent(
            anthropic_client,
            spec,
            tenant_id=TENANT_ID,
            dry_run=False,
            account_id=ACCOUNT_ID,
            public_url=None,
        )
        agents = await anthropic_client.beta.agents.list()  # pyright: ignore[reportUnknownMemberType]
        match = next((a for a in agents.data if a.name == name), None)
        assert match is not None, f"agent {name!r} not found after reconcile"
        md = dict(match.metadata or {})
        assert md.get("daimon_tenant") == str(TENANT_ID), (
            f"daimon_tenant not stamped (got {md.get('daimon_tenant')!r})"
        )
        assert md.get("daimon_name") == name, (
            f"daimon_name not stamped (got {md.get('daimon_name')!r})"
        )
        assert md.get("daimon_account") == str(ACCOUNT_ID), (
            f"daimon_account not stamped (got {md.get('daimon_account')!r})"
        )
        assert match.id.startswith("agent_"), f"id shape unexpected: {match.id!r}"
    finally:
        await _cleanup(anthropic_client, name)


@pytest.mark.asyncio
async def test_fork_agent_creates_distinct_ma_agents_with_same_system_prompt(
    anthropic_client: AsyncAnthropic,
) -> None:
    """`fork` path: reconcile twice with the same spec base but different names."""
    src_name = f"phase35-fork-src-{RUN_TAG}"
    fork_name = f"phase35-fork-copy-{RUN_TAG}"
    src_spec = AgentSpec.model_validate(
        {"name": src_name, "model": "claude-sonnet-4-6", "system": "phase 35 fork source"}
    )
    fork_spec = src_spec.model_copy(deep=True).model_copy(update={"name": fork_name})
    try:
        await reconcile_agent(
            anthropic_client,
            src_spec,
            tenant_id=TENANT_ID,
            dry_run=False,
            account_id=ACCOUNT_ID,
            public_url=None,
        )
        await reconcile_agent(
            anthropic_client,
            fork_spec,
            tenant_id=TENANT_ID,
            dry_run=False,
            account_id=ACCOUNT_ID,
            public_url=None,
        )
        agents = await anthropic_client.beta.agents.list()  # pyright: ignore[reportUnknownMemberType]
        names = {a.name for a in agents.data}
        assert src_name in names and fork_name in names, (
            f"missing one of fork pair: have {sorted(names)[:5]}..."
        )
        src_match = next(a for a in agents.data if a.name == src_name)
        fork_match = next(a for a in agents.data if a.name == fork_name)
        assert src_match.system == fork_match.system, "fork did not preserve system prompt"
        assert src_match.id != fork_match.id, "fork created only one agent (id collision)"
    finally:
        await _cleanup(anthropic_client, src_name, fork_name)


@pytest.mark.asyncio
async def test_mcp_modal_path_roundtrips_after_bug_25_03_fix(
    anthropic_client: AsyncAnthropic,
) -> None:
    """`mcp` path: panel-shaped spec (mcp_servers + matching mcp_toolset) reconciles cleanly.

    The panel's reducer adds both an ``mcp_servers`` entry and a matching
    ``mcp_toolset`` entry in ``tools``, so the well-formed spec must roundtrip
    against live MA.
    """
    name = f"phase35-mcp-{RUN_TAG}"
    well_formed_spec = AgentSpec.model_validate(
        {
            "name": name,
            "model": "claude-sonnet-4-6",
            "system": "phase 35 mcp",
            "mcp_servers": [{"name": "test-mcp", "type": "url", "url": "https://example.com/mcp"}],
            "tools": [
                {
                    "type": "mcp_toolset",
                    "mcp_server_name": "test-mcp",
                    "default_config": {"permission_policy": {"type": "always_allow"}},
                }
            ],
        }
    )
    try:
        await reconcile_agent(
            anthropic_client,
            well_formed_spec,
            tenant_id=TENANT_ID,
            dry_run=False,
            account_id=ACCOUNT_ID,
            public_url=None,
        )
        agents = await anthropic_client.beta.agents.list()  # pyright: ignore[reportUnknownMemberType]
        match = next((a for a in agents.data if a.name == name), None)
        assert match is not None, f"agent {name!r} not found after reconcile"
        mcp_names = [s.name for s in (match.mcp_servers or [])]
        assert "test-mcp" in mcp_names, f"mcp_servers did not round-trip: got {mcp_names}"
        toolset_refs: list[str] = [
            getattr(t, "mcp_server_name", "")
            for t in (match.tools or [])
            if getattr(t, "type", None) == "mcp_toolset"
        ]
        assert "test-mcp" in toolset_refs, (
            f"mcp_toolset cross-reference did not round-trip: got {toolset_refs}"
        )
    finally:
        await _cleanup(anthropic_client, name)
