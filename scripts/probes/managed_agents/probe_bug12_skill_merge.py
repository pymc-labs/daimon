"""Live probe for Bug #39 (skill side of #12): `defaults apply` must NOT
clobber user-pinned external skills on the default daimon agent.

Procedure mirrors probe_bug12_default_agent_merge.py but on the skills
axis. Picks a non-`cli-auth` skill from the workspace skill catalog,
pins it to the live daimon agent via SDK, runs `reconcile_agent`,
asserts the skill survives, then unpins.

Run:
    source .env.fly
    uv run python scripts/probes/managed_agents/probe_bug12_skill_merge.py
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

    print("== Bug #39 (skill merge) live probe ==")
    print(f"  public_url = {public_url}")

    # Discover tenant_id.
    tenant_id: uuid.UUID | None = None
    async for agent in client.beta.agents.list(limit=20):
        md = agent.metadata or {}
        if md.get("daimon_name") == "daimon" and md.get("daimon_tenant"):
            tenant_id = uuid.UUID(md["daimon_tenant"])
            break
    if tenant_id is None:
        print("ERROR: no daimon-tagged agent on this workspace", file=sys.stderr)
        return 2
    print(f"  tenant_id = {tenant_id}")

    matches = await find_agents_by_daimon_tag(client, tenant_id=tenant_id, name="daimon")
    if not matches:
        print("ERROR: no live daimon agent", file=sys.stderr)
        return 2
    live = matches[0]
    print(f"  live daimon agent id = {live.id} version = {live.version}")
    print(f"  current skills = {[s.skill_id for s in live.skills]}")

    # Pick a workspace skill that isn't already pinned to the agent. Skip
    # whatever is already there (which after resolution includes cli-auth
    # as `skill_xxx`). We just need any unrelated skill to prove user-pinned
    # skills survive reconcile.
    already_pinned = {s.skill_id for s in live.skills}
    print(f"  already pinned (ma ids) = {already_pinned}")

    probe_skill_id: str | None = None
    probe_skill_type: str | None = None
    async for sk in client.beta.skills.list(limit=50):
        if sk.id in already_pinned:
            continue
        # SkillListResponse.type is always "skill"; the agent skill kind
        # (anthropic|custom) is on `source`.
        probe_skill_id = sk.id
        probe_skill_type = sk.source
        break
    if probe_skill_id is None:
        print("ERROR: no non-spec skill available on workspace to use as probe", file=sys.stderr)
        return 2
    print(f"  probe_skill_id = {probe_skill_id} (type={probe_skill_type})")

    # Attach probe skill via SDK update.
    new_skills = [{"skill_id": s.skill_id, "type": s.type} for s in live.skills]
    new_skills.append({"skill_id": probe_skill_id, "type": probe_skill_type})

    print(f"  attaching {probe_skill_id} via SDK update ...")
    attached = await client.beta.agents.update(
        live.id,
        version=live.version,
        skills=new_skills,  # type: ignore[arg-type]
    )
    print(
        f"  after attach: skills = {[s.skill_id for s in attached.skills]} "
        f"(version={attached.version})"
    )
    if probe_skill_id not in [s.skill_id for s in attached.skills]:
        print("ERROR: probe setup failed — skill not attached", file=sys.stderr)
        return 2

    try:
        print("  running reconcile_agent (mirrors `daimon defaults apply`) ...")
        outcome = await reconcile_agent(
            client,
            daimon_spec,
            tenant_id=tenant_id,
            dry_run=False,
            public_url=public_url,
        )
        print(f"  reconcile outcome: {outcome.action.value} (id={outcome.anthropic_id})")

        after = await client.beta.agents.retrieve(live.id)
        after_ids = [s.skill_id for s in after.skills]
        print(f"  after reconcile: skills = {after_ids}")

        if probe_skill_id not in after_ids:
            print(
                f"\nFAIL: Bug #39 NOT fixed — {probe_skill_id!r} was clobbered.",
                file=sys.stderr,
            )
            return 1

        print(f"\nPASS: Bug #39 fix verified — {probe_skill_id!r} survived reconcile.")
        return 0

    finally:
        print(f"\n== Cleanup: unpinning {probe_skill_id} ==")
        try:
            current = await client.beta.agents.retrieve(live.id)
            cleaned = [
                {"skill_id": s.skill_id, "type": s.type}
                for s in current.skills
                if s.skill_id != probe_skill_id
            ]
            await client.beta.agents.update(
                current.id,
                version=current.version,
                skills=cleaned,  # type: ignore[arg-type]
            )
            print("  cleanup ok")
        except anthropic.APIError as err:
            print(f"  WARNING: cleanup failed: {err}", file=sys.stderr)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
