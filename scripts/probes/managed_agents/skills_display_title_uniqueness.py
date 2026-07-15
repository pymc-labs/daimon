"""Probe: does MA enforce display_title uniqueness on skills?

Question: Can two skills be created with the same display_title? If so,
display_title is NOT a reliable identity anchor — two tenants (or two runs)
on the same Anthropic workspace could collide when using
display_title="daimon-system:<name>" as the reconciliation key.

Methodology: DESTRUCTIVE. Creates two skills with the same display_title,
records whether the second create succeeds or raises, then deletes both.

WARNING: This probe creates (and then deletes) real skill resources in your
Anthropic workspace. Do NOT run this against a production workspace unless
you are certain about the consequences. It will leave no resources behind if
it runs to completion, but a mid-run crash could leave orphaned skills that
you will need to delete manually via the API or dashboard.
"""

from __future__ import annotations

import asyncio
import io
import os
import zipfile

from anthropic import AsyncAnthropic
from dotenv import load_dotenv

DISPLAY_TITLE = "probe-uniqueness-test"

SKILL_MD_CONTENT = """\
---
name: probe-test
description: Probe test skill
---
This is a test skill for probing.
"""


def _make_skill_zip() -> bytes:
    """Build a minimal skill zip in memory: probe-test/SKILL.md"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("probe-test/SKILL.md", SKILL_MD_CONTENT)
    return buf.getvalue()


async def main() -> None:
    load_dotenv()
    api_key = os.environ.get("ANTHROPIC_API_KEY") or os.environ.get(
        "DAIMON_ANTHROPIC__API_KEY"
    )
    if not api_key:
        raise RuntimeError("Set ANTHROPIC_API_KEY or DAIMON_ANTHROPIC__API_KEY")

    client = AsyncAnthropic(api_key=api_key)

    skill_zip = _make_skill_zip()
    created_ids: list[str] = []

    # ── Step 1: create first skill ────────────────────────────────────────────
    print(f"== Step 1: create skill A with display_title={DISPLAY_TITLE!r} ==")
    skill_a = await client.beta.skills.create(
        display_title=DISPLAY_TITLE,
        files=[("probe-test.zip", skill_zip, "application/zip")],
    )
    created_ids.append(skill_a.id)
    print(f"  skill A created: id={skill_a.id!r}")
    print()

    # ── Step 2: attempt to create second skill with the same display_title ────
    print(f"== Step 2: create skill B with SAME display_title={DISPLAY_TITLE!r} ==")
    skill_b_id: str | None = None
    second_create_error: Exception | None = None

    try:
        skill_b = await client.beta.skills.create(
            display_title=DISPLAY_TITLE,
            files=[("probe-test.zip", skill_zip, "application/zip")],
        )
        skill_b_id = skill_b.id
        created_ids.append(skill_b.id)
        print(f"  skill B created: id={skill_b.id!r}")
        print("  RESULT: second create SUCCEEDED — display_title is NOT unique")
    except Exception as e:
        second_create_error = e
        print(f"  second create FAILED: {type(e).__name__}: {e}")
        print("  RESULT: display_title IS enforced as unique (or an unrelated error)")
    print()

    # ── Step 3: cleanup ───────────────────────────────────────────────────────
    print("== Step 3: cleanup — deleting created skills ==")
    for skill_id in created_ids:
        try:
            async for ver in client.beta.skills.versions.list(skill_id, limit=100):
                try:
                    await client.beta.skills.versions.delete(ver.version, skill_id=skill_id)
                    print(f"  deleted version {ver.version!r} from {skill_id!r}")
                except Exception as e:
                    print(f"  WARNING: failed to delete version: {type(e).__name__}: {e}")
            await client.beta.skills.delete(skill_id)
            print(f"  deleted: {skill_id!r}")
        except Exception as e:
            print(f"  WARNING: failed to delete {skill_id!r}: {type(e).__name__}: {e}")
    print()

    # ── Conclusion ────────────────────────────────────────────────────────────
    print("== CONCLUSION ==")
    if skill_b_id is not None:
        print("  display_title uniqueness: NOT ENFORCED by MA")
        print(f"  Both skills were created with display_title={DISPLAY_TITLE!r}")
        print(f"  skill A id: {skill_a.id!r}")
        print(f"  skill B id: {skill_b_id!r}")
        print()
        print("  COLLISION RISK: YES")
        print("  Two tenants (or two seeding runs) on the same Anthropic workspace")
        print("  can create distinct skills with the same display_title.")
        print("  display_title alone is NOT a safe identity anchor for reconciliation.")
        print("  The open-source codebase must use a different uniqueness strategy")
        print("  (e.g. list-and-match by display_title, then update-in-place, or")
        print("  store the MA skill_id in the local DB after first creation).")
    else:
        print("  display_title uniqueness: ENFORCED by MA (or create errored)")
        if second_create_error is not None:
            print(f"  Error type: {type(second_create_error).__name__}")
            print(f"  Error detail: {second_create_error}")
        print()
        print("  COLLISION RISK: LOW (MA rejects duplicate display_title)")
        print("  display_title=daimon-system:<name> is a safe identity anchor,")
        print("  provided MA returns a stable error on duplicate rather than a 2xx.")


if __name__ == "__main__":
    asyncio.run(main())
