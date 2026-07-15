"""Probe: does MA allow display_title to differ from SKILL.md frontmatter `name:`?

Question: When you call `client.beta.skills.create(display_title="outer", files=[...])`
with a zip whose SKILL.md has `name: inner`, does MA:

  A. Reject the mismatch (4xx)?
  B. Silently use one value (which one — the API param or the file)?
  C. Store both independently (display_title=outer, name=inner)?

This matters for daimon's defaults/skills/ reconciliation. If MA enforces that
`display_title` on the API call must match `name:` in the SKILL.md, we have to
keep them in sync. If MA stores them independently, we need to understand which
field is authoritative for matching and retrieval.

Methodology: DESTRUCTIVE. Creates a single skill then cleans up. If interrupted,
a skill with display_title "probe-mismatch-outer" may remain in the workspace.

Requires: ANTHROPIC_API_KEY (or DAIMON_ANTHROPIC__API_KEY) set in environment
or a .env file. The key must have write access to the workspace.
"""

from __future__ import annotations

import asyncio
import io
import os
import uuid
import zipfile

from anthropic import APIStatusError, AsyncAnthropic
from dotenv import load_dotenv

# The two values we deliberately mismatch:
OUTER_DISPLAY_TITLE = "probe-mismatch-outer"
INNER_SKILL_NAME = "probe-mismatch-inner"


def _build_mismatched_skill_zip() -> bytes:
    """Build a skill zip where SKILL.md `name:` disagrees with the API display_title.

    The directory name inside the zip also uses the inner name so we can separately
    observe whether MA cares about the directory name vs the frontmatter vs the API param.
    """
    skill_md = f"""\
---
name: {INNER_SKILL_NAME}
display_title: {INNER_SKILL_NAME}
description: Probe skill for name vs display_title mismatch test.
---

# {INNER_SKILL_NAME}

This skill was created by scripts/probes/managed_agents/skill_name_mismatch.py.
The API call used display_title={OUTER_DISPLAY_TITLE!r} but this file has name={INNER_SKILL_NAME!r}.
It should be deleted automatically after the probe completes.
""".encode()

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(f"{INNER_SKILL_NAME}/SKILL.md", skill_md)
    return buf.getvalue()


def _shape_summary(obj: object, indent: int = 4) -> str:
    """Return a human-readable field/value summary for a Pydantic model."""
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
        raise RuntimeError(
            "Set ANTHROPIC_API_KEY or DAIMON_ANTHROPIC__API_KEY to run this probe."
        )

    client = AsyncAnthropic(api_key=api_key)

    tag = uuid.uuid4().hex[:8]
    outer_title = f"{OUTER_DISPLAY_TITLE}-{tag}"
    inner_name = f"{INNER_SKILL_NAME}-{tag}"

    print("=== PROBE: skill name vs display_title mismatch ===")
    print(f"  API display_title : {outer_title!r}")
    print(f"  SKILL.md name     : {inner_name!r}")
    print(f"  SKILL.md dir name : {inner_name!r}  (matches SKILL.md name, not API param)")
    print()

    skill_id: str | None = None

    try:
        # ── Step 1: Create skill with mismatched display_title vs SKILL.md name ─
        print("=== 1. Create skill (display_title != SKILL.md name) ===")

        # Build the zip with the inner name baked in
        skill_md = f"""\
---
name: {inner_name}
display_title: {inner_name}
description: Probe skill for name vs display_title mismatch test.
---

# {inner_name}

This skill was created by scripts/probes/managed_agents/skill_name_mismatch.py.
The API call used display_title={outer_title!r} but this file has name={inner_name!r}.
""".encode()
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(f"{inner_name}/SKILL.md", skill_md)
        zip_bytes = buf.getvalue()

        try:
            created = await client.beta.skills.create(
                display_title=outer_title,
                files=[("skill.zip", zip_bytes, "application/zip")],
            )
            skill_id = created.id
            print(f"  CREATE SUCCEEDED — skill_id={skill_id!r}")
            print(f"  Full create() response shape:")
            print(_shape_summary(created))
        except APIStatusError as err:
            body = err.response.json() if err.response.content else {}
            error_obj = body.get("error", {})
            print(f"  CREATE REJECTED — status={err.status_code}")
            print(f"  error.type={error_obj.get('type')!r}")
            print(f"  error.message={error_obj.get('message')!r}")
            print()
            print("VERDICT: MA rejects a mismatch between display_title and SKILL.md name.")
            return

        # ── Step 2: Check what display_title shows in skills.list() ──────────
        print()
        print("=== 2. List skills — what does display_title show? ===")
        found_in_list: object | None = None
        async for sk in client.beta.skills.list(limit=100):
            if sk.id == skill_id:
                found_in_list = sk
                break

        if found_in_list is None:
            print(f"  WARNING: skill {skill_id!r} not found in skills.list() — unexpected.")
        else:
            list_display_title = getattr(found_in_list, "display_title", "<MISSING>")
            list_name = getattr(found_in_list, "name", "<MISSING>")
            print(f"  list entry for skill_id={skill_id!r}:")
            print(f"    display_title : {list_display_title!r}")
            print(f"    name          : {list_name!r}")
            print(f"  Full list entry shape:")
            print(_shape_summary(found_in_list, indent=4))

        # ── Step 3: Retrieve the skill and check what name shows ──────────────
        print()
        print("=== 3. Retrieve skill — what does skills.retrieve() show? ===")
        try:
            retrieved = await client.beta.skills.retrieve(skill_id)
            retrieved_display_title = getattr(retrieved, "display_title", "<MISSING>")
            retrieved_name = getattr(retrieved, "name", "<MISSING>")
            print(f"  skills.retrieve({skill_id!r}):")
            print(f"    display_title : {retrieved_display_title!r}")
            print(f"    name          : {retrieved_name!r}")
            print(f"  Full retrieve() response shape:")
            print(_shape_summary(retrieved))
        except APIStatusError as err:
            print(f"  ERROR: status={err.status_code} body={err.response.text!r}")

        # ── Step 4: Retrieve the latest version and check name there ──────────
        print()
        print("=== 4. Inspect skill version — what does versions.list() show? ===")
        versions: list[object] = []
        async for ver in client.beta.skills.versions.list(skill_id, limit=100):
            versions.append(ver)

        if not versions:
            print("  WARNING: no versions returned for this skill.")
        else:
            for i, ver in enumerate(versions, 1):
                ver_id = getattr(ver, "version", "<MISSING>")
                ver_name = getattr(ver, "name", "<MISSING>")
                ver_display_title = getattr(ver, "display_title", "<MISSING>")
                print(f"  version [{i}] id={ver_id!r}:")
                print(f"    name          : {ver_name!r}")
                print(f"    display_title : {ver_display_title!r}")
                print(f"  Full version shape:")
                print(_shape_summary(ver, indent=4))

        # ── Step 5: Retrieve a specific version if possible ───────────────────
        if versions:
            latest_ver = versions[0]
            ver_id = getattr(latest_ver, "version", None)
            if ver_id:
                print()
                print(f"=== 5. versions.retrieve({ver_id!r}) — full version detail ===")
                try:
                    ver_detail = await client.beta.skills.versions.retrieve(
                        ver_id, skill_id=skill_id
                    )
                    ver_detail_name = getattr(ver_detail, "name", "<MISSING>")
                    ver_detail_display_title = getattr(ver_detail, "display_title", "<MISSING>")
                    print(f"  name          : {ver_detail_name!r}")
                    print(f"  display_title : {ver_detail_display_title!r}")
                    print(f"  Full version detail shape:")
                    print(_shape_summary(ver_detail))
                except APIStatusError as err:
                    print(f"  ERROR: status={err.status_code} body={err.response.text!r}")
                except Exception as e:  # noqa: BLE001
                    print(f"  NOTE: versions.retrieve() not available or failed: {type(e).__name__}: {e}")

        # ── Step 6: Verdict ───────────────────────────────────────────────────
        print()
        print("=== VERDICT ===")
        print(f"  API display_title used at create : {outer_title!r}")
        print(f"  SKILL.md name:                   : {inner_name!r}")
        print()

        if found_in_list is not None:
            list_dt = getattr(found_in_list, "display_title", "<MISSING>")
            if list_dt == outer_title:
                print("  skills.list() display_title => API param wins (outer)")
            elif list_dt == inner_name:
                print("  skills.list() display_title => SKILL.md name wins (inner)")
            else:
                print(f"  skills.list() display_title => UNEXPECTED value: {list_dt!r}")

        if versions:
            ver_dt = getattr(versions[0], "display_title", "<MISSING>")
            ver_nm = getattr(versions[0], "name", "<MISSING>")
            if ver_nm == inner_name and ver_dt == outer_title:
                print("  versions.list() => BOTH stored independently")
                print("    display_title (API param) and name (SKILL.md) are separate fields.")
                print("    IMPLICATION: MA does not enforce consistency between them.")
                print("    daimon must decide which field to use for matching (display_title).")
            elif ver_nm == "<MISSING>" and ver_dt == outer_title:
                print("  versions.list() => only display_title present (API param wins)")
                print("    name field absent from version objects.")
            elif ver_nm == inner_name and ver_dt == "<MISSING>":
                print("  versions.list() => only name present (SKILL.md wins)")
                print("    display_title field absent from version objects.")
            else:
                print(f"  versions.list() name={ver_nm!r}, display_title={ver_dt!r}")
                print("  IMPLICATION: inspect full output above to determine behavior.")

    finally:
        # ── Cleanup ───────────────────────────────────────────────────────────
        print()
        print("=== Cleanup ===")
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
        else:
            print("  no skill to clean up (create was rejected or skill_id not captured)")
        print("  cleanup done")


if __name__ == "__main__":
    asyncio.run(main())
