"""Probe: characterize agent.update field semantics.

Q1 — PATCH vs PUT:
  Create an agent with name, model, system, description, tools, mcp_servers.
  Call update() with ONLY name, model, system. Do description/tools/mcp_servers
  survive (PATCH) or get cleared (PUT)?

Q2 — retrieve-then-update roundtrip:
  Call retrieve() on an agent, dump the returned object back into update().
  Do any fields need to be stripped or translated to avoid errors?

Run:
    uv run python scripts/probes/managed_agents/update_field_semantics.py
"""

from __future__ import annotations

import asyncio
import os
import uuid

from anthropic import AsyncAnthropic
from dotenv import load_dotenv


def fmt(v: object) -> str:
    if v is None:
        return "None"
    if isinstance(v, list):
        return f"[{len(v)} items]" if v else "[]"
    return repr(v)


async def probe_patch_vs_put(client: AsyncAnthropic) -> None:
    print("\n== Q1: PATCH vs PUT semantics ==")
    suffix = uuid.uuid4().hex[:8]

    agent = await client.beta.agents.create(
        name=f"probe-fldtest-{suffix}",
        model={"id": "claude-haiku-4-5", "speed": "standard"},
        system="hello",
        description="original description",
        tools=[{"type": "custom", "name": "probe_tool", "description": "probe", "input_schema": {"type": "object", "properties": {}}}],
        mcp_servers=[],
        skills=[],
    )
    print(f"  created agent={agent.id} version={agent.version}")
    print(f"  initial: description={fmt(agent.description)} tools={fmt(agent.tools)} mcp_servers={fmt(agent.mcp_servers)}")

    # Update passing ONLY name, model, system — omit description, tools, mcp_servers entirely.
    updated = await client.beta.agents.update(
        agent.id,
        version=agent.version,
        name=agent.name,
        model={"id": "claude-haiku-4-5", "speed": "standard"},
        system="world",
    )
    print(f"\n  after update(name, model, system only):")
    print(f"    system       : {fmt(updated.system)}")
    print(f"    description  : {fmt(updated.description)}")
    print(f"    tools        : {fmt(updated.tools)}")
    print(f"    mcp_servers  : {fmt(updated.mcp_servers)}")

    desc_preserved = updated.description == "original description"
    tools_preserved = bool(updated.tools)  # had 1 item

    print(f"\n  CONCLUSION: description preserved={desc_preserved}, tools preserved={tools_preserved}")
    if desc_preserved and tools_preserved:
        print("  -> PATCH semantics: omitted fields are left unchanged")
    elif not desc_preserved and not tools_preserved:
        print("  -> PUT semantics: omitted fields cleared/nulled")
    else:
        print("  -> MIXED: some fields preserved, some cleared — inspect above")

    return agent.id, updated.version


async def probe_roundtrip(client: AsyncAnthropic, agent_id: str, version: int) -> None:
    print("\n== Q2: retrieve-then-update roundtrip ==")

    retrieved = await client.beta.agents.retrieve(agent_id)
    print(f"  retrieved agent={retrieved.id} version={retrieved.version}")
    print(f"  retrieved fields: {[k for k in retrieved.model_fields_set]}")

    # Dump everything from the retrieved object and attempt update.
    # We'll try naively first, then strip known read-only fields if it fails.
    stripped_fields: list[str] = []

    def build_kwargs(agent, skip: set[str]) -> dict:
        """Extract all non-None settable fields from the retrieved agent."""
        candidates = {
            "name": agent.name,
            "model": agent.model,
            "system": agent.system,
            "description": agent.description,
            "tools": agent.tools,
            "mcp_servers": agent.mcp_servers,
            "skills": agent.skills if hasattr(agent, "skills") else None,
        }
        return {k: v for k, v in candidates.items() if v is not None and k not in skip}

    skip: set[str] = set()
    last_error: Exception | None = None
    result = None

    for attempt in range(6):
        kwargs = build_kwargs(retrieved, skip)
        try:
            result = await client.beta.agents.update(
                agent_id,
                version=version,
                **kwargs,
            )
            print(f"  attempt {attempt + 1}: OK (skipped={sorted(skip) or 'none'})")
            break
        except Exception as e:
            last_error = e
            err_str = str(e)
            print(f"  attempt {attempt + 1}: {type(e).__name__}: {err_str[:200]}")

            # Heuristic: identify the offending field from the error message and retry.
            offender = None
            for field in list(kwargs.keys()):
                if field in err_str.lower():
                    offender = field
                    break

            if offender:
                skip.add(offender)
                stripped_fields.append(offender)
                print(f"    -> will retry without {offender!r}")
            else:
                # Can't identify field; give up.
                break

    if result is not None:
        print(f"\n  Roundtrip succeeded. Fields that had to be stripped: {stripped_fields or 'none'}")
        if stripped_fields:
            print(f"  CONCLUSION: read-only/computed fields that must be excluded: {stripped_fields}")
        else:
            print("  CONCLUSION: retrieve() fields map directly into update() — no stripping needed")
    else:
        print(f"\n  Roundtrip FAILED after all attempts. Last error: {last_error}")
        print(f"  Fields stripped before giving up: {stripped_fields}")

    return result


async def main() -> None:
    load_dotenv()
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise SystemExit("ANTHROPIC_API_KEY not set")

    client = AsyncAnthropic()

    agent_id = None
    try:
        agent_id, version = await probe_patch_vs_put(client)
        await probe_roundtrip(client, agent_id, version)
    finally:
        if agent_id:
            await client.beta.agents.archive(agent_id)
            print(f"\n  archived {agent_id}")


if __name__ == "__main__":
    asyncio.run(main())
