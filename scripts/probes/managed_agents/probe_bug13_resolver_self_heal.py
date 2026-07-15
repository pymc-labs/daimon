"""Live probe for Bug #13: resolver self-heal must produce an agent with
daimon-mcp attached.

Setup
-----
    source .env.fly
    # ensure `flyctl mpg proxy <CLUSTER_ID> -p 5433` is running
    uv run python scripts/probes/managed_agents/probe_bug13_resolver_self_heal.py

Procedure
---------
1. Find the live daimon agent.
2. Archive it via SDK.
3. Call `resolve_agent(public_url=settings.mcp.public_url)` — mimics the
   scheduler self-heal path.
4. Retrieve the recreated agent. Assert `mcp_servers` contains a
   `daimon-mcp` entry whose URL == settings.mcp.public_url.

NOTE: This mutates the live workspace. After the probe runs successfully,
the new daimon agent IS the live one. The scheduler will pick it up via
tag-lookup; the routine row's cached `agent_id` will self-heal on its
next fire (probe #10's exact mechanism).
"""

from __future__ import annotations

import asyncio
import os
import sys
import uuid
from pathlib import Path

from anthropic import AsyncAnthropic
from daimon.core.config import load_settings
from daimon.core.defaults.ma_index import find_agents_by_daimon_tag
from daimon.core.ma_resolver import resolve_agent
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


async def main() -> int:
    settings = load_settings()
    public_url = str(settings.mcp.public_url) if settings.mcp.public_url else None
    if not public_url:
        print("ERROR: settings.mcp.public_url not set", file=sys.stderr)
        return 2

    api_key = settings.anthropic.api_key.get_secret_value()
    client = AsyncAnthropic(api_key=api_key)

    print("== Bug #13 live probe ==")
    print(f"  public_url = {public_url}")

    # Discover tenant.
    tenant_id: uuid.UUID | None = None
    async for agent in client.beta.agents.list(limit=20):
        md = agent.metadata or {}
        if md.get("daimon_name") == "daimon" and md.get("daimon_tenant"):
            tenant_id = uuid.UUID(md["daimon_tenant"])
            break
    if tenant_id is None:
        print("ERROR: no daimon-tagged agent to derive tenant", file=sys.stderr)
        return 2
    print(f"  tenant_id = {tenant_id}")

    matches = await find_agents_by_daimon_tag(client, tenant_id=tenant_id, name="daimon")
    if not matches:
        print("ERROR: no live daimon agent", file=sys.stderr)
        return 2
    old = matches[0]
    print(f"  archiving live daimon agent {old.id} (version {old.version}) ...")
    await client.beta.agents.archive(old.id)

    # Engine for resolver's apply_defaults bootstrap.
    db_url = os.environ["DAIMON_DATABASE__URL"]
    engine = create_async_engine(db_url)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    defaults_root = Path(__file__).resolve().parents[3] / "defaults"

    try:
        print("  invoking resolve_agent (self-heal path) ...")
        new_id = await resolve_agent(
            client,
            tenant_id=tenant_id,
            daimon_tag="daimon",
            session_factory=session_factory,
            defaults_root=defaults_root,
            public_url=public_url,
        )
        print(f"  resolved agent id = {new_id}")

        after = await client.beta.agents.retrieve(new_id)
        servers = [(s.name, s.url) for s in after.mcp_servers]
        print(f"  after self-heal: mcp_servers = {servers}")

        daimon_mcp = next((s for s in after.mcp_servers if s.name == "daimon-mcp"), None)
        if daimon_mcp is None:
            print(
                "\nFAIL: Bug #13 NOT fixed — self-healed agent missing daimon-mcp.",
                file=sys.stderr,
            )
            return 1
        if daimon_mcp.url != public_url:
            print(
                f"\nFAIL: daimon-mcp URL mismatch — got {daimon_mcp.url!r}, "
                f"expected {public_url!r}",
                file=sys.stderr,
            )
            return 1

        print(
            f"\nPASS: Bug #13 fix verified — self-healed agent has "
            f"daimon-mcp at {public_url}"
        )
        return 0
    finally:
        await engine.dispose()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
