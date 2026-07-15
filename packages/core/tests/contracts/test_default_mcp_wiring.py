"""Env-gated live-MA integration test: apply_defaults injects the default MCP URL.

Satisfies Phase 34 success criterion #4: dump the spec MA holds for a freshly
seeded agent (client.beta.agents.retrieve(agent_id)) and assert the default
mcp_servers entry is present.

Skipped when DAIMON_TEST_ANTHROPIC_API_KEY is unset — never gates CI
(per guideline:testing — live API tests run locally before PRs only).
"""

from __future__ import annotations

import contextlib
import os
from pathlib import Path

import pytest
from anthropic import AsyncAnthropic
from daimon.core.defaults import apply_defaults
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

pytestmark = pytest.mark.contract

DEFAULT_MCP_URL = "https://daimon-mcp-test.example.com/mcp"


@pytest.mark.skipif(
    not os.environ.get("DAIMON_TEST_ANTHROPIC_API_KEY"),
    reason="live MA contract test; set DAIMON_TEST_ANTHROPIC_API_KEY to enable",
)
async def test_apply_defaults_injects_default_mcp_into_seeded_agent_spec(
    anthropic_client: AsyncAnthropic,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    # Build a minimal defaults tree (one agent yaml, no skills, no envs).
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    (agents_dir / "test-mcp-wiring.yaml").write_text(
        "name: test-mcp-wiring\n"
        "model: claude-sonnet-4-6\n"
        "system: integration test agent for MCP-01 wiring\n"
    )

    created_agent_ids: list[str] = []
    try:
        report = await apply_defaults(
            db_session_factory,
            anthropic_client,
            tmp_path,
            dry_run=False,
            public_url=DEFAULT_MCP_URL,
        )
        agent_outcomes = [o for o in report.agents if o.name == "test-mcp-wiring"]
        assert len(agent_outcomes) == 1, "exactly one agent outcome expected"
        agent_id = agent_outcomes[0].anthropic_id
        assert agent_id is not None, "apply_defaults should record the MA-side anthropic_id"
        created_agent_ids.append(agent_id)

        retrieved = await anthropic_client.beta.agents.retrieve(agent_id)
        urls = [s.url for s in (retrieved.mcp_servers or [])]
        assert DEFAULT_MCP_URL in urls, (
            f"retrieved agent's mcp_servers must contain the default URL; got {urls!r}"
        )
        # The mcp_toolset cross-reference is what MA validates server-side.
        mcp_toolsets = [
            t for t in (retrieved.tools or []) if getattr(t, "type", None) == "mcp_toolset"
        ]
        toolset_names = [getattr(t, "mcp_server_name", None) for t in mcp_toolsets]
        assert "daimon-mcp" in toolset_names, (
            f"retrieved agent's tools must contain an mcp_toolset for daimon-mcp; got {toolset_names!r}"
        )

        # Idempotency: re-apply and confirm the URL still appears exactly once.
        await apply_defaults(
            db_session_factory,
            anthropic_client,
            tmp_path,
            dry_run=False,
            public_url=DEFAULT_MCP_URL,
        )
        retrieved2 = await anthropic_client.beta.agents.retrieve(agent_id)
        urls2 = [s.url for s in (retrieved2.mcp_servers or [])]
        assert urls2.count(DEFAULT_MCP_URL) == 1, (
            f"re-apply must not duplicate the default URL; got {urls2!r}"
        )
        mcp_toolsets2 = [
            t
            for t in (retrieved2.tools or [])
            if getattr(t, "type", None) == "mcp_toolset"
            and getattr(t, "mcp_server_name", None) == "daimon-mcp"
        ]
        assert len(mcp_toolsets2) == 1, (
            f"re-apply must not duplicate the daimon-mcp mcp_toolset; got {mcp_toolsets2!r}"
        )
    finally:
        for aid in created_agent_ids:
            with contextlib.suppress(Exception):
                await anthropic_client.beta.agents.archive(aid)
