"""MA wire-shape probes backing the 2026-04-23 CLI smoke-test findings.

Four sweeps:
  P1. `beta.sessions.events.send` initial `user.message` content shape.
  P2. Skill zip layouts accepted by `beta.skills.create`.
  P3. `user.tool_confirmation` event shape (field name + value type).
  P4. `beta.agents.create` with `agent_toolset_20260401` — captures pydantic
      serialization warnings and MA accept/reject verdict.

Run from repo root with a live workspace API key:

    set -a; source .env; set +a
    uv run python scripts/probes/managed_agents/cli_shape_drift.py

AGENT_ID / ENV_ID default to the ones seeded by the smoke-test session on
2026-04-23; override via env vars PROBE_AGENT_ID / PROBE_ENV_ID if those have
been archived.

Probes create throwaway sessions and skills. Skills are deleted on success.
Sessions are left behind (no delete endpoint); they time out via idle.

Each probe prints `[OK]` when MA accepted the shape and `[400]` with the
trimmed error message otherwise. Shape validation errors surface as
`events.0.<field>: ...`; semantic errors (e.g. unknown tool_use_id) indicate
the shape validated and the request reached business logic.
"""

from __future__ import annotations

import asyncio
import io
import os
import warnings
import zipfile

from anthropic import AsyncAnthropic, BadRequestError
from anthropic.types.beta import BetaManagedAgentsAgentToolset20260401Params

AGENT_ID = os.environ.get("PROBE_AGENT_ID", "agent_011CaK9Hyyyg6fZ4hefZ3Vxy")
ENV_ID = os.environ.get("PROBE_ENV_ID", "env_01Tzk36PnGAnY5QySDJYYCWG")
AGENT_VERSION = int(os.environ.get("PROBE_AGENT_VERSION", "1"))


def banner(label: str) -> None:
    print(f"\n==== {label} ====")


async def new_session(c: AsyncAnthropic) -> str:
    s = await c.beta.sessions.create(
        agent={"type": "agent", "id": AGENT_ID, "version": AGENT_VERSION},
        environment_id=ENV_ID,
    )
    return s.id


async def try_send(c: AsyncAnthropic, label: str, events: list) -> None:
    sid = await new_session(c)
    try:
        await c.beta.sessions.events.send(sid, events=events)
        print(f"  [OK]  {label}  session={sid}")
    except BadRequestError as e:
        msg = str(e)
        i = msg.find("message':")
        if i > -1:
            msg = msg[i : i + 240]
        print(f"  [400] {label}  -> {msg[:260]}")


async def try_create_skill(c: AsyncAnthropic, label: str, buf: io.BytesIO) -> None:
    try:
        r = await c.beta.skills.create(
            display_title=f"probe-{label[:40].replace(' ', '-')}",
            files=[("SKILL.zip", buf, "application/zip")],
        )
        print(f"  [OK]  {label}  -> {r.id}")
        try:
            await c.beta.skills.delete(r.id)
        except Exception:
            pass
    except BadRequestError as e:
        msg = str(e)
        i = msg.find("message':")
        if i > -1:
            msg = msg[i : i + 250]
        print(f"  [400] {label}  -> {msg[:260]}")


def zip_with(contents: dict[str, bytes]) -> io.BytesIO:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in contents.items():
            z.writestr(name, data)
    buf.seek(0)
    return buf


SKILL_MD = b"""---
name: probe
description: probe
---
# Probe

Content.
"""


async def main() -> None:
    c = AsyncAnthropic(api_key=os.environ["DAIMON_ANTHROPIC__API_KEY"])

    banner("P1. events.send initial user.message content shape")
    await try_send(
        c,
        "string-content (current driver.py)",
        [{"type": "user.message", "content": "hi"}],
    )
    await try_send(
        c,
        "array-of-text-block",
        [{"type": "user.message", "content": [{"type": "text", "text": "hi"}]}],
    )
    await try_send(c, "empty array", [{"type": "user.message", "content": []}])
    await try_send(
        c,
        "multi-text-blocks",
        [
            {
                "type": "user.message",
                "content": [
                    {"type": "text", "text": "hello"},
                    {"type": "text", "text": "world"},
                ],
            }
        ],
    )

    banner("P2. skill zip layouts")
    await try_create_skill(c, "root-SKILL.md", zip_with({"SKILL.md": SKILL_MD}))
    await try_create_skill(c, "top-folder-SKILL.md", zip_with({"probe/SKILL.md": SKILL_MD}))
    await try_create_skill(
        c,
        "top-folder-with-subfile",
        zip_with({"probe/SKILL.md": SKILL_MD, "probe/notes.txt": b"notes"}),
    )
    await try_create_skill(
        c, "nested-only-no-root-SKILL.md", zip_with({"probe/sub/SKILL.md": SKILL_MD})
    )
    await try_create_skill(
        c, "mismatched-folder-name", zip_with({"other/SKILL.md": SKILL_MD})
    )

    banner("P3. user.tool_confirmation shape")
    fake = "toolu_fake"
    variants: list[tuple[str, dict]] = [
        (
            "allow=true (current driver.py:124)",
            {"type": "user.tool_confirmation", "tool_use_id": fake, "allow": True},
        ),
        (
            "decision=allow",
            {"type": "user.tool_confirmation", "tool_use_id": fake, "decision": "allow"},
        ),
        (
            "approved=true",
            {"type": "user.tool_confirmation", "tool_use_id": fake, "approved": True},
        ),
        (
            "result=allow (accepted shape)",
            {"type": "user.tool_confirmation", "tool_use_id": fake, "result": "allow"},
        ),
        (
            "result=deny (accepted shape)",
            {"type": "user.tool_confirmation", "tool_use_id": fake, "result": "deny"},
        ),
        (
            "result=approved (wrong enum value)",
            {"type": "user.tool_confirmation", "tool_use_id": fake, "result": "approved"},
        ),
        (
            "result={allow:true} (wrong type)",
            {"type": "user.tool_confirmation", "tool_use_id": fake, "result": {"allow": True}},
        ),
    ]
    for tag, event in variants:
        await try_send(c, tag, [event])

    banner("P4. agents.create agent_toolset_20260401 shape + warnings")
    await probe_agents_create(c)


async def try_agent_create(
    c: AsyncAnthropic,
    label: str,
    *,
    tools: list,
    configs_as_generator: bool = False,
) -> None:
    """Create a throwaway agent; record pydantic warnings and MA verdict.

    Deletes on success via beta.agents.archive (no hard delete endpoint).
    """
    import uuid as _uuid

    name = f"probe-{label[:30].replace(' ', '-')}-{_uuid.uuid4().hex[:6]}"
    create_kwargs = {
        "name": name,
        "model": "claude-sonnet-4-5",
        "system": "probe",
        "tools": (x for x in tools) if configs_as_generator else tools,
    }
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        try:
            r = await c.beta.agents.create(**create_kwargs)
            serialization_warnings = [
                str(w.message)
                for w in caught
                if "PydanticSerializationUnexpectedValue" in type(w.message).__name__
                or "PydanticSerializationUnexpectedValue" in str(w.message)
                or "serializer" in str(w.message).lower()
            ]
            if serialization_warnings:
                print(f"  [OK+warn] {label}  -> {r.id}")
                for w in serialization_warnings:
                    print(f"    ⚠ {w[:300]}")
            else:
                print(f"  [OK]  {label}  -> {r.id}  (no serialization warnings)")
            try:
                await c.beta.agents.archive(r.id)
            except Exception:
                pass
        except BadRequestError as e:
            msg = str(e)
            i = msg.find("message':")
            if i > -1:
                msg = msg[i : i + 260]
            print(f"  [400] {label}  -> {msg[:300]}")


async def probe_agents_create(c: AsyncAnthropic) -> None:
    # Variant A: dict-shape tools (exactly what reconcile_agents.py emits today
    # after spec.model_dump(exclude_none=True) on a YAML-loaded AgentSpec).
    await try_agent_create(
        c,
        "dict-tools (current reconcile shape)",
        tools=[
            {
                "type": "agent_toolset_20260401",
                "configs": [{"name": "bash"}, {"name": "read"}],
            }
        ],
    )

    # Variant B: fully-typed SDK param object.
    typed_toolset: BetaManagedAgentsAgentToolset20260401Params = {
        "type": "agent_toolset_20260401",
        "configs": [{"name": "bash"}, {"name": "read"}],
    }
    await try_agent_create(c, "typed-toolset (SDK param TypedDict)", tools=[typed_toolset])

    # Variant C: configs as generator (the findings doc's #7 bullet).
    await try_agent_create(
        c,
        "configs-as-list (baseline)",
        tools=[
            {
                "type": "agent_toolset_20260401",
                "configs": [{"name": "bash"}],
            }
        ],
    )


if __name__ == "__main__":
    asyncio.run(main())
