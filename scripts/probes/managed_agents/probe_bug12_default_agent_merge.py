"""Live probe for Bug #12: `defaults apply` must NOT clobber user mutations
on the default daimon agent.

Setup
-----
    source .env.fly
    uv run python scripts/probes/managed_agents/probe_bug12_default_agent_merge.py

Procedure
---------
1. Find the live daimon agent on staging (by daimon_tag).
2. Snapshot mcp_servers + skills.
3. Attach an external MCP entry directly via SDK (mcp_servers union).
4. Call `reconcile_agent` against the live workspace.
5. Re-retrieve the agent. Assert the external MCP entry is still present.
6. Cleanup: remove the external MCP entry.

Exits non-zero if any assertion fails.
"""

from __future__ import annotations

import asyncio
import os
import sys
import uuid
from pathlib import Path

import anthropic
from anthropic import AsyncAnthropic
from daimon.core.defaults.loader import load_agent_specs
from daimon.core.defaults.ma_index import find_agents_by_daimon_tag
from daimon.core.defaults.reconcile_agents import reconcile_agent

PROBE_MCP_NAME = "probe-bug12-mcp"
PROBE_MCP_URL = "https://probe.example.invalid/mcp"


async def main() -> int:
    api_key = os.environ.get("ANTHROPIC_API_KEY") or os.environ.get(
        "DAIMON_ANTHROPIC__API_KEY"
    )
    if not api_key:
        print("ERROR: Set ANTHROPIC_API_KEY or DAIMON_ANTHROPIC__API_KEY", file=sys.stderr)
        return 2

    public_url = os.environ.get("DAIMON_MCP__PUBLIC_URL")

    defaults_root = Path(__file__).resolve().parents[3] / "defaults"
    agent_specs = load_agent_specs(defaults_root / "agents")
    daimon_spec = next((s for s in agent_specs if s.name == "daimon"), None)
    if daimon_spec is None:
        print("ERROR: defaults/agents/daimon.yaml not found", file=sys.stderr)
        return 2

    client = AsyncAnthropic(api_key=api_key)

    print("== Bug #12 live probe ==")
    print(f"  public_url = {public_url}")

    # Discover tenant_id by finding any daimon-tagged agent and reading its metadata.
    print("  discovering tenant_id from existing daimon agent ...")
    tenant_id: uuid.UUID | None = None
    async for agent in client.beta.agents.list(limit=20):
        md = agent.metadata or {}
        if md.get("daimon_name") == "daimon" and md.get("daimon_tenant"):
            tenant_id = uuid.UUID(md["daimon_tenant"])
            break
    if tenant_id is None:
        print("ERROR: no daimon-tagged agent on this workspace to derive tenant", file=sys.stderr)
        return 2
    print(f"  tenant_id = {tenant_id}")

    # 1. Find live daimon agent.
    matches = await find_agents_by_daimon_tag(client, tenant_id=tenant_id, name="daimon")
    if not matches:
        print("ERROR: no live daimon agent found on this workspace", file=sys.stderr)
        return 2
    live = matches[0]
    print(f"  live daimon agent id = {live.id} version = {live.version}")
    print(
        f"  current mcp_servers = {[s.name for s in live.mcp_servers]}"
    )

    # 2. Attach external MCP via SDK update. Union with whatever is there.
    new_servers = [
        {"name": s.name, "type": s.type, "url": s.url} for s in live.mcp_servers
    ]
    if PROBE_MCP_NAME not in [s["name"] for s in new_servers]:
        new_servers.append(
            {"name": PROBE_MCP_NAME, "type": "url", "url": PROBE_MCP_URL}
        )
    # MA requires an mcp_toolset entry for every mcp_server.
    new_tools = []
    for t in live.tools:
        if t.type == "mcp_toolset":
            new_tools.append(
                {
                    "type": "mcp_toolset",
                    "mcp_server_name": t.mcp_server_name,
                    "default_config": {
                        "enabled": True,
                        "permission_policy": {"type": "always_allow"},
                    },
                }
            )
    if PROBE_MCP_NAME not in [t.get("mcp_server_name") for t in new_tools]:
        new_tools.append(
            {
                "type": "mcp_toolset",
                "mcp_server_name": PROBE_MCP_NAME,
                "default_config": {
                    "enabled": True,
                    "permission_policy": {"type": "always_allow"},
                },
            }
        )

    print(f"  attaching {PROBE_MCP_NAME} via SDK update ...")
    attached = await client.beta.agents.update(
        live.id,
        version=live.version,
        mcp_servers=new_servers,  # type: ignore[arg-type]
        tools=new_tools,  # type: ignore[arg-type]
    )
    print(
        f"  after attach: mcp_servers = {[s.name for s in attached.mcp_servers]} "
        f"(version={attached.version})"
    )
    assert PROBE_MCP_NAME in [s.name for s in attached.mcp_servers], (
        "probe setup failed: external MCP not present after SDK update"
    )

    try:
        # 3. Call reconcile_agent — the exact code path defaults apply runs.
        print("  running reconcile_agent (mirrors `daimon defaults apply`) ...")
        outcome = await reconcile_agent(
            client,
            daimon_spec,
            tenant_id=tenant_id,
            dry_run=False,
            public_url=public_url,
        )
        print(f"  reconcile outcome: {outcome.action.value} (id={outcome.anthropic_id})")

        # 4. Re-fetch agent and assert.
        after = await client.beta.agents.retrieve(live.id)
        after_names = [s.name for s in after.mcp_servers]
        print(f"  after reconcile: mcp_servers = {after_names}")

        if PROBE_MCP_NAME not in after_names:
            print(
                f"\nFAIL: Bug #12 NOT fixed — {PROBE_MCP_NAME!r} was clobbered.",
                file=sys.stderr,
            )
            return 1

        print(f"\nPASS: Bug #12 fix verified — {PROBE_MCP_NAME!r} survived reconcile.")
        return 0

    finally:
        # 5. Cleanup: remove the probe MCP entry.
        print(f"\n== Cleanup: removing {PROBE_MCP_NAME} ==")
        try:
            current = await client.beta.agents.retrieve(live.id)
            cleaned_servers = [
                {"name": s.name, "type": s.type, "url": s.url}
                for s in current.mcp_servers
                if s.name != PROBE_MCP_NAME
            ]
            cleaned_tools = []
            for t in current.tools:
                if t.type == "mcp_toolset" and t.mcp_server_name == PROBE_MCP_NAME:
                    continue
                if t.type == "mcp_toolset":
                    cleaned_tools.append(
                        {
                            "type": "mcp_toolset",
                            "mcp_server_name": t.mcp_server_name,
                            "default_config": {
                                "enabled": True,
                                "permission_policy": {"type": "always_allow"},
                            },
                        }
                    )
            await client.beta.agents.update(
                current.id,
                version=current.version,
                mcp_servers=cleaned_servers,  # type: ignore[arg-type]
                tools=cleaned_tools,  # type: ignore[arg-type]
            )
            print("  cleanup ok")
        except anthropic.APIError as err:
            print(f"  WARNING: cleanup failed: {err}", file=sys.stderr)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
