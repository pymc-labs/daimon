"""Probe: what happens when you hard-delete a skill that an agent references?

DESTRUCTIVE WARNING: This probe creates real resources (skill + agent) in your
MA workspace and then attempts to delete the skill. It cleans up after itself,
but if the process is interrupted mid-run, orphan resources may remain with the
name prefix "probe-delete-ref-".

Question answered: does MA reject skills.delete() with 409/400 when an agent
still references that skill, or does it succeed silently and leave the agent in
a broken state?

Methodology:
  1. Create a temporary skill (minimal SKILL.md zip, built inline).
  2. Create a temporary agent that references the skill.
  3. Attempt skills.delete() while the agent still holds the reference.
  4. Record the outcome: status code, error message, or success.
  5. If delete succeeded: retrieve the agent and inspect its skills list.
  6. Cleanup: archive the agent, delete the skill if it still exists.

Requires: ANTHROPIC_API_KEY (or DAIMON_ANTHROPIC__API_KEY) set in the
environment or a .env file. The key must have write access to the workspace.
"""

from __future__ import annotations

import asyncio
import io
import os
import uuid
import zipfile

from anthropic import APIStatusError, AsyncAnthropic
from dotenv import load_dotenv


def _build_minimal_skill_zip(skill_name: str) -> bytes:
    """Build an MA-compatible skill zip in-memory.

    MA requires a single top-level directory whose name matches the skill.
    The directory must contain a SKILL.md. We build this inline with stdlib
    zipfile — no daimon.core imports.
    """
    buf = io.BytesIO()
    skill_md = f"""---
name: {skill_name}
display_title: {skill_name}
description: Minimal probe skill for testing delete-with-agent-refs behavior.
---

# {skill_name}

This is a minimal probe skill created by scripts/probes/managed_agents/skills_delete_with_agent_refs.py.
It should be deleted automatically after the probe completes.
""".encode()

    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(f"{skill_name}/SKILL.md", skill_md)

    return buf.getvalue()


async def main() -> None:
    load_dotenv()
    api_key = os.environ.get("ANTHROPIC_API_KEY") or os.environ.get(
        "DAIMON_ANTHROPIC__API_KEY"
    )
    if not api_key:
        raise RuntimeError(
            "DESTRUCTIVE PROBE: Set ANTHROPIC_API_KEY or DAIMON_ANTHROPIC__API_KEY "
            "to run this probe. It creates and deletes real MA resources."
        )

    client = AsyncAnthropic(api_key=api_key)

    tag = uuid.uuid4().hex[:8]
    skill_name = f"probe-delete-ref-{tag}"
    agent_name = f"probe-delete-ref-agent-{tag}"

    skill_id: str | None = None
    agent_id: str | None = None

    try:
        # ── Step 1: Create the probe skill ───────────────────────────────────
        print(f"=== 1. Create probe skill (name={skill_name!r}) ===")
        zip_bytes = _build_minimal_skill_zip(skill_name)
        created_skill = await client.beta.skills.create(
            display_title=skill_name,
            files=[("skill.zip", zip_bytes, "application/zip")],
        )
        skill_id = created_skill.id
        print(f"  skill_id={skill_id!r}")
        print(f"  display_title={getattr(created_skill, 'display_title', '?')!r}")

        # ── Step 2: Create an agent that references the skill ─────────────────
        print(f"\n=== 2. Create probe agent (name={agent_name!r}) referencing skill ===")
        created_agent = await client.beta.agents.create(
            model="claude-sonnet-4-6",
            name=agent_name,
            skills=[{"type": "custom", "skill_id": skill_id}],
            metadata={"daimon_probe": "skills_delete_with_agent_refs"},
        )
        agent_id = created_agent.id
        agent_version = getattr(created_agent, "version", None)
        print(f"  agent_id={agent_id!r}")
        print(f"  agent_version={agent_version!r}")
        agent_skills = getattr(created_agent, "skills", [])
        print(f"  agent.skills on create: {agent_skills!r}")

        # ── Step 3: Attempt to delete the skill while the agent references it ─
        print(f"\n=== 3. Attempt skills.delete({skill_id!r}) while agent references it ===")
        delete_succeeded = False
        delete_status: int | None = None
        delete_error_type: str | None = None
        delete_error_message: str | None = None

        try:
            await client.beta.skills.delete(skill_id)
            delete_succeeded = True
            skill_id = None  # MA accepted the delete — skill is gone
            print("  DELETE SUCCEEDED (200/204)")
            print("  MA did NOT reject the delete despite agent reference.")
        except APIStatusError as err:
            delete_status = err.status_code
            body = err.response.json() if err.response.content else {}
            error_obj = body.get("error", {})
            delete_error_type = error_obj.get("type")
            delete_error_message = error_obj.get("message")
            print(f"  DELETE REJECTED: status={delete_status}")
            print(f"  error.type={delete_error_type!r}")
            print(f"  error.message={delete_error_message!r}")

        # ── Step 4: If delete succeeded, inspect the agent's skills list ──────
        if delete_succeeded:
            print(f"\n=== 4. Retrieve agent after skill deletion ===")
            try:
                retrieved_agent = await client.beta.agents.retrieve(agent_id)
                post_delete_skills = getattr(retrieved_agent, "skills", "<MISSING>")
                print(f"  agent.skills after skill delete: {post_delete_skills!r}")
                if not post_delete_skills:
                    print("  OBSERVATION: agent.skills is now empty/null — silent breakage.")
                else:
                    print("  OBSERVATION: agent.skills still populated after skill delete.")
            except APIStatusError as err:
                print(f"  ERROR retrieving agent: status={err.status_code} body={err.response.text!r}")
        else:
            print("\n=== 4. Skipped (delete was rejected — agent and skill both intact) ===")

        # ── Step 5: Verdict ───────────────────────────────────────────────────
        print("\n=== VERDICT ===")
        if delete_succeeded:
            print("  RESULT: MA ALLOWS hard-deleting a skill that an agent references.")
            print("  The delete succeeds silently. Agent may be left in a broken state.")
            print("  Implication: daimon must never rely on MA to enforce referential integrity.")
            print("  Skill-deletion logic must check agent references before calling skills.delete().")
        else:
            print(f"  RESULT: MA REJECTS skills.delete() when an agent references the skill.")
            print(f"  Status: {delete_status}, type: {delete_error_type!r}")
            print(f"  Message: {delete_error_message!r}")
            print("  Implication: MA enforces referential integrity. daimon must handle this error")
            print("  (remove skill from agent first, or surface the constraint to the operator).")

    finally:
        # ── Cleanup ───────────────────────────────────────────────────────────
        print("\n=== Cleanup ===")

        if agent_id:
            try:
                await client.beta.agents.archive(agent_id)
                print(f"  archived agent {agent_id!r}")
            except Exception as e:  # noqa: BLE001
                print(f"  WARNING: failed to archive agent {agent_id!r}: {e}")

        if skill_id:
            try:
                async for ver in client.beta.skills.versions.list(skill_id, limit=100):
                    try:
                        await client.beta.skills.versions.delete(ver.version, skill_id=skill_id)
                        print(f"  deleted version {ver.version!r}")
                    except Exception as e:  # noqa: BLE001
                        print(f"  WARNING: failed to delete version {ver.version!r}: {e}")
                await client.beta.skills.delete(skill_id)
                print(f"  deleted skill {skill_id!r}")
            except Exception as e:  # noqa: BLE001
                print(f"  WARNING: failed to delete skill {skill_id!r}: {e}")

        print("  cleanup done")


if __name__ == "__main__":
    asyncio.run(main())
