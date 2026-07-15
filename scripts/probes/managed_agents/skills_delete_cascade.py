"""Probe: does skills.delete() cascade-delete versions, or must you delete versions manually first?

DESTRUCTIVE WARNING: Creates a real skill + version in your MA workspace, then
attempts to hard-delete the skill WITHOUT deleting the version first. Cleans up
after itself, but if interrupted mid-run an orphan skill named
"probe-delete-cascade-*" may remain.

Question answered: when you call skills.delete(skill_id) while the skill still
has one or more versions, does MA:
  (a) succeed and implicitly delete all versions (cascade), or
  (b) reject the request with an error (you must delete versions first)?

Methodology:
  1. Create a minimal skill (single-entry zip: SKILL.md with frontmatter).
  2. Push a second version via skills.versions.create() so the skill definitely
     has >=1 explicit version beyond the one created at skill-creation time.
  3. Confirm the version exists via skills.versions.list().
  4. Call skills.delete() WITHOUT touching versions first.
  5. Record the outcome: success or error details.
  6. If delete succeeded, attempt skills.versions.list() on the now-deleted skill
     to confirm versions are also gone.
  7. Cleanup: if delete failed, manually delete versions then skill.

Requires: ANTHROPIC_API_KEY (or DAIMON_ANTHROPIC__API_KEY) set in the
environment or a .env file.
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
    The directory must contain a SKILL.md.
    """
    buf = io.BytesIO()
    skill_md = f"""---
name: {skill_name}
display_title: {skill_name}
description: Minimal probe skill for testing delete-cascade behavior.
---

# {skill_name}

This is a minimal probe skill created by scripts/probes/managed_agents/skills_delete_cascade.py.
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
    skill_name = f"probe-delete-cascade-{tag}"
    skill_id: str | None = None

    try:
        # ── Step 1: Create the skill (this implicitly creates version 1) ────────
        print(f"=== 1. Create skill (name={skill_name!r}) ===")
        zip_bytes = _build_minimal_skill_zip(skill_name)
        created = await client.beta.skills.create(
            display_title=skill_name,
            files=[("skill.zip", zip_bytes, "application/zip")],
        )
        skill_id = created.id
        print(f"  skill_id={skill_id!r}")
        print(f"  display_title={getattr(created, 'display_title', '?')!r}")

        # ── Step 2: Push a second version ────────────────────────────────────────
        print(f"\n=== 2. Push a second version via skills.versions.create() ===")
        same_zip = _build_minimal_skill_zip(skill_name)
        new_ver = await client.beta.skills.versions.create(
            skill_id=skill_id,
            files=[("skill.zip", same_zip, "application/zip")],
        )
        print(f"  new version id={getattr(new_ver, 'version', '?')!r}")

        # ── Step 3: Confirm versions exist ───────────────────────────────────────
        print(f"\n=== 3. List versions (before delete attempt) ===")
        versions_before: list[object] = []
        async for ver in client.beta.skills.versions.list(skill_id, limit=100):
            versions_before.append(ver)
            print(f"  version={getattr(ver, 'version', '?')!r}")
        print(f"  total versions: {len(versions_before)}")

        # ── Step 4: Attempt skills.delete() WITHOUT deleting versions first ──────
        print(f"\n=== 4. Attempt skills.delete({skill_id!r}) WITHOUT deleting versions first ===")
        delete_succeeded = False
        delete_status: int | None = None
        delete_error_type: str | None = None
        delete_error_message: str | None = None

        try:
            await client.beta.skills.delete(skill_id)
            delete_succeeded = True
            print("  DELETE SUCCEEDED (200/204)")
        except APIStatusError as err:
            delete_status = err.status_code
            body = err.response.json() if err.response.content else {}
            error_obj = body.get("error", {})
            delete_error_type = error_obj.get("type")
            delete_error_message = error_obj.get("message")
            print(f"  DELETE REJECTED: status={delete_status}")
            print(f"  error.type={delete_error_type!r}")
            print(f"  error.message={delete_error_message!r}")

        # ── Step 5: If delete succeeded, verify versions are also gone ───────────
        if delete_succeeded:
            print(f"\n=== 5. List versions on now-deleted skill (expect error or empty) ===")
            try:
                surviving_versions: list[object] = []
                async for ver in client.beta.skills.versions.list(skill_id, limit=100):
                    surviving_versions.append(ver)
                    print(f"  version still present: {getattr(ver, 'version', '?')!r}")
                if surviving_versions:
                    print(f"  WARNING: {len(surviving_versions)} version(s) still accessible on deleted skill!")
                else:
                    print("  versions list is empty — versions gone with the skill")
                skill_id = None  # deleted
            except APIStatusError as err:
                print(f"  skills.versions.list() raised {err.status_code} — skill truly gone")
                skill_id = None  # deleted
        else:
            print("\n=== 5. Skipped (delete was rejected — skill and versions still exist) ===")

        # ── Verdict ───────────────────────────────────────────────────────────────
        print("\n=== VERDICT ===")
        if delete_succeeded:
            print("  CASCADE: YES")
            print("  skills.delete() succeeded without pre-deleting versions.")
            print("  MA implicitly removes all versions when the skill is deleted.")
            print("  Implication: no need to iterate and delete versions manually before skills.delete().")
        else:
            print("  CASCADE: NO")
            print(f"  skills.delete() was rejected (status={delete_status}) while versions exist.")
            print(f"  You must delete all versions first before deleting the skill.")
            print(f"  Implication: deletion logic must enumerate and delete versions before calling skills.delete().")

    finally:
        # ── Cleanup ───────────────────────────────────────────────────────────────
        print("\n=== Cleanup ===")
        if skill_id:
            # delete failed — manually clean up versions then skill
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
        else:
            print("  skill already deleted — nothing to clean up")
        print("  cleanup done")


if __name__ == "__main__":
    asyncio.run(main())
