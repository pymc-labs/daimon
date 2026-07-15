"""Probe: does MA accept skills=[] on agent create and agent update?

2026-04-23 — Batch C "Patch C — Drop seeded skills" plans to stop seeding
the `brainstorming` skill, leaving reconcile_agents.py passing skills=[] to
both agents.create and agents.update. This probe verifies whether MA 400s on
an empty skills list. If it does, Patch C must use conditional kwarg assembly
(omit `skills` key entirely when the list is empty).

Cases probed:
  1. Create with skills=[]
  2. Create with skills omitted (control — to detect behavioral difference)
  3. Update existing agent to skills=[] (requires a skill to exist in workspace)

Cleanup: all probe-created agents are archived in a finally block.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, field

import anthropic
from anthropic import AsyncAnthropic
from dotenv import load_dotenv

PROBE_TAG = "probe:agent_skills_empty:2026-04-23"


@dataclass
class CaseResult:
    name: str
    accepted: bool
    agent_id: str | None = None
    error_status: int | None = None
    error_type: str | None = None
    error_message: str | None = None
    notes: str = ""


def _verdict_str(r: CaseResult) -> str:
    if r.accepted:
        return "ACCEPTED"
    parts = ["REJECTED"]
    if r.error_status:
        parts.append(f"status={r.error_status}")
    if r.error_type:
        parts.append(f"type={r.error_type}")
    if r.error_message:
        # Truncate long messages for table readability.
        msg = r.error_message[:80].replace("\n", " ")
        parts.append(f"msg={msg!r}")
    return " ".join(parts)


async def case_create_skills_empty(client: AsyncAnthropic) -> CaseResult:
    """Case 1: agents.create with skills=[]."""
    print("  [case 1] agents.create(skills=[]) …")
    try:
        ag = await client.beta.agents.create(
            model="claude-haiku-4-5",
            name="probe-skills-empty-create",
            skills=[],
            metadata={"probe": PROBE_TAG},
        )
        print(f"    -> created agent {ag.id}")
        return CaseResult(name="create skills=[]", accepted=True, agent_id=ag.id)
    except anthropic.BadRequestError as e:
        print(f"    -> 400 BadRequestError: {e}")
        return CaseResult(
            name="create skills=[]",
            accepted=False,
            error_status=e.status_code,
            error_type=getattr(e, "type", None),
            error_message=str(e),
        )
    except anthropic.APIStatusError as e:
        print(f"    -> APIStatusError {e.status_code}: {e}")
        return CaseResult(
            name="create skills=[]",
            accepted=False,
            error_status=e.status_code,
            error_type=getattr(e, "type", None),
            error_message=str(e),
        )


async def case_create_skills_omitted(client: AsyncAnthropic) -> CaseResult:
    """Case 2 (control): agents.create with skills kwarg omitted entirely."""
    print("  [case 2] agents.create(skills omitted) …")
    try:
        ag = await client.beta.agents.create(
            model="claude-haiku-4-5",
            name="probe-skills-omitted-create",
            metadata={"probe": PROBE_TAG},
        )
        print(f"    -> created agent {ag.id}")
        return CaseResult(name="create skills omitted", accepted=True, agent_id=ag.id)
    except anthropic.BadRequestError as e:
        print(f"    -> 400 BadRequestError: {e}")
        return CaseResult(
            name="create skills omitted",
            accepted=False,
            error_status=e.status_code,
            error_type=getattr(e, "type", None),
            error_message=str(e),
        )
    except anthropic.APIStatusError as e:
        print(f"    -> APIStatusError {e.status_code}: {e}")
        return CaseResult(
            name="create skills omitted",
            accepted=False,
            error_status=e.status_code,
            error_type=getattr(e, "type", None),
            error_message=str(e),
        )


async def case_update_skills_empty(
    client: AsyncAnthropic, skill_id: str | None
) -> CaseResult:
    """Case 3: agents.update(agent_id, skills=[]).

    Requires a skill to exist. Creates a temporary agent with that skill, then
    updates it to skills=[]. If no skills exist in workspace, skips.
    """
    if skill_id is None:
        print("  [case 3] SKIPPED — no skills in workspace to create agent with")
        return CaseResult(
            name="update skills=[]",
            accepted=False,
            notes="SKIPPED — no skills in workspace",
        )

    base_agent_id: str | None = None
    base_agent_version: int | None = None
    print(f"  [case 3] creating base agent with skill {skill_id!r} …")
    try:
        base_ag = await client.beta.agents.create(
            model="claude-haiku-4-5",
            name="probe-skills-empty-update-base",
            skills=[{"skill_id": skill_id, "type": "custom"}],
            metadata={"probe": PROBE_TAG},
        )
        base_agent_id = base_ag.id
        base_agent_version = base_ag.version  # type: ignore[attr-defined]
        print(f"    -> created base agent {base_agent_id} version={base_agent_version}")
    except anthropic.APIStatusError as e:
        print(f"    -> failed to create base agent: {e.status_code}: {e}")
        return CaseResult(
            name="update skills=[]",
            accepted=False,
            error_status=e.status_code,
            error_message=str(e),
            notes="base agent create failed",
        )

    try:
        print(f"  [case 3] agents.update({base_agent_id}, skills=[]) …")
        await client.beta.agents.update(base_agent_id, version=base_agent_version, skills=[])
        print("    -> update accepted")
        return CaseResult(
            name="update skills=[]", accepted=True, agent_id=base_agent_id
        )
    except anthropic.BadRequestError as e:
        print(f"    -> 400 BadRequestError: {e}")
        return CaseResult(
            name="update skills=[]",
            accepted=False,
            agent_id=base_agent_id,
            error_status=e.status_code,
            error_type=getattr(e, "type", None),
            error_message=str(e),
        )
    except anthropic.APIStatusError as e:
        print(f"    -> APIStatusError {e.status_code}: {e}")
        return CaseResult(
            name="update skills=[]",
            accepted=False,
            agent_id=base_agent_id,
            error_status=e.status_code,
            error_type=getattr(e, "type", None),
            error_message=str(e),
        )


def _print_table(results: list[CaseResult]) -> None:
    print()
    print(f"{'CASE':<30} {'RESULT'}")
    print("-" * 90)
    for r in results:
        if r.notes.startswith("SKIPPED"):
            print(f"{r.name:<30} SKIPPED ({r.notes})")
        else:
            print(f"{r.name:<30} {_verdict_str(r)}")
    print()


def _overall_verdict(results: list[CaseResult]) -> str:
    """Derive the key question: can Patch C safely pass skills=[] unconditionally?"""
    create_empty = next((r for r in results if r.name == "create skills=[]"), None)
    create_omit = next((r for r in results if r.name == "create skills omitted"), None)
    update_empty = next((r for r in results if r.name == "update skills=[]"), None)

    lines: list[str] = []

    # Case 1 verdict.
    if create_empty:
        if create_empty.accepted:
            lines.append("  create skills=[]: ACCEPTED")
        else:
            lines.append(
                f"  create skills=[]: REJECTED"
                f" (status={create_empty.error_status}, msg={create_empty.error_message!r})"
            )

    # Case 2 verdict + comparison.
    if create_omit:
        if create_omit.accepted:
            lines.append("  create skills omitted: ACCEPTED")
        else:
            lines.append(
                f"  create skills omitted: REJECTED"
                f" (status={create_omit.error_status})"
            )

    if create_empty and create_omit:
        if create_empty.accepted == create_omit.accepted:
            lines.append("  skills=[] vs omitted: SAME behavior")
        else:
            lines.append(
                "  skills=[] vs omitted: DIFFERENT behavior — conditional kwarg assembly needed"
            )

    # Case 3 verdict.
    if update_empty:
        if update_empty.notes.startswith("SKIPPED"):
            lines.append("  update skills=[]: SKIPPED (no skills in workspace)")
        elif update_empty.accepted:
            lines.append("  update skills=[]: ACCEPTED")
        else:
            lines.append(
                f"  update skills=[]: REJECTED"
                f" (status={update_empty.error_status}, msg={update_empty.error_message!r})"
            )

    # Final recommendation.
    create_ok = create_empty is not None and create_empty.accepted
    update_ok = (
        update_empty is None
        or update_empty.notes.startswith("SKIPPED")
        or update_empty.accepted
    )

    if create_ok and update_ok:
        lines.append(
            "  RECOMMENDATION: Patch C can safely pass skills=[] unconditionally."
        )
    elif not create_ok and update_ok:
        lines.append(
            "  RECOMMENDATION: Patch C MUST conditionalize — omit skills key when list is empty (create rejects it)."
        )
    elif create_ok and not update_ok:
        lines.append(
            "  RECOMMENDATION: Patch C MUST conditionalize — omit skills key when list is empty (update rejects it)."
        )
    else:
        lines.append(
            "  RECOMMENDATION: Patch C MUST conditionalize — omit skills key when list is empty (both create and update reject it)."
        )

    return "\n".join(lines)


async def main() -> None:
    load_dotenv()
    api_key = os.environ.get("ANTHROPIC_API_KEY") or os.environ.get(
        "DAIMON_ANTHROPIC__API_KEY"
    )
    if not api_key:
        raise RuntimeError("Set ANTHROPIC_API_KEY or DAIMON_ANTHROPIC__API_KEY")

    client = AsyncAnthropic(api_key=api_key)

    # Collect all created agent IDs for cleanup.
    created_agent_ids: list[str] = []
    results: list[CaseResult] = []

    # ── Discover an existing skill (for case 3) ───────────────────────────────
    print("== Discovering existing skills (limit=10) ==")
    first_skill_id: str | None = None
    async for sk in client.beta.skills.list(limit=10):
        first_skill_id = sk.id
        print(f"  found skill: id={sk.id!r} name={getattr(sk, 'name', '?')!r}")
        break
    if first_skill_id is None:
        print("  WARNING: no skills found in workspace — case 3 will be skipped")

    try:
        # ── Case 1: create with skills=[] ─────────────────────────────────────
        print("\n== Case 1: agents.create(skills=[]) ==")
        r1 = await case_create_skills_empty(client)
        results.append(r1)
        if r1.agent_id:
            created_agent_ids.append(r1.agent_id)

        # ── Case 2: create with skills omitted (control) ──────────────────────
        print("\n== Case 2 (control): agents.create(skills omitted) ==")
        r2 = await case_create_skills_omitted(client)
        results.append(r2)
        if r2.agent_id:
            created_agent_ids.append(r2.agent_id)

        # ── Case 3: update existing agent to skills=[] ────────────────────────
        print("\n== Case 3: agents.update(agent_id, skills=[]) ==")
        r3 = await case_update_skills_empty(client, first_skill_id)
        results.append(r3)
        if r3.agent_id and r3.agent_id not in created_agent_ids:
            created_agent_ids.append(r3.agent_id)

        # ── Results table ─────────────────────────────────────────────────────
        print("\n== RESULTS TABLE ==")
        _print_table(results)

        # ── Verdict ───────────────────────────────────────────────────────────
        verdict = _overall_verdict(results)
        print(f"VERDICT:\n{verdict}")

    finally:
        print(f"\n== Cleanup: archiving {len(created_agent_ids)} probe agent(s) ==")
        for aid in created_agent_ids:
            try:
                await client.beta.agents.archive(aid)
                print(f"  archived {aid}")
            except Exception as e:
                print(f"  WARNING: failed to archive {aid}: {e}")
        print("  cleanup done")


if __name__ == "__main__":
    asyncio.run(main())
