"""Probe: does MA deduplicate skill versions when content is identical?

Question: If you push a new version of a skill with the exact same content as
the previous version, does MA create a new version entry, or does it
deduplicate/skip?

This matters for the `daimon skills update` command — if MA always creates a
new version regardless of content, the CLI should diff locally before pushing
to avoid version inflation.

Methodology: DESTRUCTIVE. Creates a temporary skill, pushes a second version
with identical content, lists versions, then cleans up.

WARNING: This probe creates (and then deletes) a real skill resource. If
interrupted mid-run, a skill with display_title matching
"probe-dedup-test-*" may remain in the workspace.
"""

from __future__ import annotations

import asyncio
import io
import os
import time
import zipfile

from anthropic import AsyncAnthropic
from dotenv import load_dotenv

SKILL_MD_CONTENT = """\
---
name: probe-dedup-test
display_title: probe-dedup-test
description: Minimal skill for dedup probe.
---

# probe-dedup-test

This is a minimal skill for testing version deduplication behavior.
"""


def _build_skill_zip() -> bytes:
    """Build a minimal skill zip in memory: probe-dedup-test/SKILL.md"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("probe-dedup-test/SKILL.md", SKILL_MD_CONTENT)
    return buf.getvalue()


def _shape_summary(obj: object, indent: int = 4) -> str:
    """Return a human-readable shape summary for a Pydantic model."""
    pad = " " * indent
    if hasattr(type(obj), "model_fields"):
        lines = []
        for field_name in type(obj).model_fields:
            val = getattr(obj, field_name, "<MISSING>")
            if val is None:
                lines.append(f"{pad}{field_name}: None")
            elif isinstance(val, (str, int, float, bool)):
                display = repr(val) if len(repr(val)) <= 80 else repr(val[:77]) + "…'"
                lines.append(f"{pad}{field_name}: {type(val).__name__} = {display}")
            else:
                lines.append(f"{pad}{field_name}: {type(val).__name__}")
        declared = set(type(obj).model_fields.keys())
        for k, v in vars(obj).items():
            if not k.startswith("_") and k not in declared:
                lines.append(f"{pad}[extra] {k}: {type(v).__name__} = {v!r}")
        return "\n".join(lines)
    return f"{pad}{type(obj).__name__}: {obj!r}"


async def main() -> None:
    load_dotenv()
    api_key = os.environ.get("ANTHROPIC_API_KEY") or os.environ.get(
        "DAIMON_ANTHROPIC__API_KEY"
    )
    if not api_key:
        raise RuntimeError("Set ANTHROPIC_API_KEY or DAIMON_ANTHROPIC__API_KEY")

    client = AsyncAnthropic(api_key=api_key)

    tag = str(int(time.time()))
    display_title = f"probe-dedup-test-{tag}"
    skill_zip = _build_skill_zip()
    skill_id: str | None = None

    try:
        # ── Step 1: Create the skill (first version) ─────────────────────────
        print(f"== Step 1: Create skill with display_title={display_title!r} ==")
        created = await client.beta.skills.create(
            display_title=display_title,
            files=[("skill.zip", skill_zip, "application/zip")],
        )
        skill_id = created.id
        print(f"  skill_id={skill_id!r}")
        print(f"  response shape:")
        print(_shape_summary(created))

        # Record the initial version
        initial_version = getattr(created, "latest_version", None)
        print(f"\n  initial latest_version: {initial_version!r}")
        if initial_version and hasattr(type(initial_version), "model_fields"):
            print("  initial version shape:")
            print(_shape_summary(initial_version, indent=6))

        # ── Step 2: List versions before pushing duplicate ───────────────────
        print("\n== Step 2: List versions (before duplicate push) ==")
        versions_before: list[object] = []
        async for ver in client.beta.skills.versions.list(skill_id, limit=100):
            versions_before.append(ver)
            print(f"  version: {ver.version!r}")
            print(_shape_summary(ver, indent=6))
        print(f"  total versions before: {len(versions_before)}")

        # ── Step 3: Push identical content as a new version ──────────────────
        print("\n== Step 3: Push IDENTICAL content as a new version ==")
        # Rebuild the exact same zip to ensure identical content
        same_zip = _build_skill_zip()
        new_version = await client.beta.skills.versions.create(
            skill_id=skill_id,
            files=[("skill.zip", same_zip, "application/zip")],
        )
        print(f"  versions.create() response:")
        print(_shape_summary(new_version, indent=4))

        new_version_id = getattr(new_version, "version", None)
        print(f"\n  new version id: {new_version_id!r}")

        # ── Step 4: List versions after pushing duplicate ────────────────────
        print("\n== Step 4: List versions (after duplicate push) ==")
        versions_after: list[object] = []
        async for ver in client.beta.skills.versions.list(skill_id, limit=100):
            versions_after.append(ver)
            print(f"  version: {ver.version!r}")
            print(_shape_summary(ver, indent=6))
        print(f"  total versions after: {len(versions_after)}")

        # ── Step 5: Compare ──────────────────────────────────────────────────
        print("\n== Step 5: Comparison ==")
        print(f"  versions before duplicate push: {len(versions_before)}")
        print(f"  versions after duplicate push:  {len(versions_after)}")

        before_ids = [getattr(v, "version", None) for v in versions_before]
        after_ids = [getattr(v, "version", None) for v in versions_after]
        print(f"  version IDs before: {before_ids}")
        print(f"  version IDs after:  {after_ids}")

        new_ids = [vid for vid in after_ids if vid not in before_ids]
        print(f"  new version IDs:    {new_ids}")

        # Check content hashes if available
        for ver in versions_after:
            content_hash = getattr(ver, "content_hash", None)
            checksum = getattr(ver, "checksum", None)
            if content_hash or checksum:
                print(f"  version {getattr(ver, 'version', '?')}: content_hash={content_hash!r}, checksum={checksum!r}")

        # ── Verdict ──────────────────────────────────────────────────────────
        print("\n== VERDICT ==")
        if len(versions_after) > len(versions_before):
            print("  RESULT: MA does NOT deduplicate identical content.")
            print(f"  Pushing the same content created a new version (total: {len(versions_after)}).")
            print("  IMPLICATION: `daimon skills update` should diff content locally")
            print("  before pushing to avoid creating redundant versions.")
        elif len(versions_after) == len(versions_before):
            if set(after_ids) == set(before_ids):
                print("  RESULT: MA DOES deduplicate identical content.")
                print("  No new version was created when pushing identical content.")
                print("  IMPLICATION: safe to always push — MA handles dedup.")
            else:
                print("  RESULT: AMBIGUOUS — same count but different IDs.")
                print("  MA may have replaced the existing version in-place.")
                print(f"  Before IDs: {before_ids}")
                print(f"  After IDs:  {after_ids}")
        else:
            print(f"  RESULT: UNEXPECTED — version count decreased ({len(versions_before)} -> {len(versions_after)}).")

    finally:
        # ── Cleanup ──────────────────────────────────────────────────────────
        print("\n== Cleanup ==")
        if skill_id:
            try:
                async for ver in client.beta.skills.versions.list(skill_id, limit=100):
                    try:
                        await client.beta.skills.versions.delete(
                            ver.version, skill_id=skill_id
                        )
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
