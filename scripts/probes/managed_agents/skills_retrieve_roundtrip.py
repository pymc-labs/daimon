"""Probe: what does skills.retrieve() return, and can it round-trip to skills.create()?

Question for the skills-fork design: does retrieving a skill return the content
(zip / source files) in a form that can be POSTed back to create a fork? Or is
content absent, forcing fork to read from local defaults/skills/?

Methodology: read-only. Lists all skills, retrieves each one, prints the full
response shape. No create/delete calls.

Expected workspace (per 2026-04-23 smoke): four Anthropic built-ins
(pdf/docx/xlsx/pptx) — source="anthropic", id is a bare name, not skill_* shape.
If user-created skills are present they will also appear.
"""

from __future__ import annotations

import asyncio
import os

from anthropic import AsyncAnthropic
from dotenv import load_dotenv


def _shape_summary(obj: object, indent: int = 4) -> str:
    """Return a human-readable shape summary for a Pydantic model / dict."""
    pad = " " * indent
    if hasattr(type(obj), "model_fields"):
        # Pydantic BaseModel — inspect declared fields and values
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
        # also check for extra fields via __dict__
        declared = set(type(obj).model_fields.keys())
        for k, v in vars(obj).items():
            if not k.startswith("_") and k not in declared:
                lines.append(f"{pad}[extra] {k}: {type(v).__name__} = {v!r}")
        return "\n".join(lines)
    elif isinstance(obj, dict):
        lines = []
        for k, v in obj.items():
            lines.append(f"{pad}{k!r}: {type(v).__name__} = {v!r}")
        return "\n".join(lines)
    else:
        return f"{pad}{type(obj).__name__}: {obj!r}"


async def main() -> None:
    load_dotenv()
    api_key = os.environ.get("ANTHROPIC_API_KEY") or os.environ.get(
        "DAIMON_ANTHROPIC__API_KEY"
    )
    if not api_key:
        raise RuntimeError("Set ANTHROPIC_API_KEY or DAIMON_ANTHROPIC__API_KEY")

    client = AsyncAnthropic(api_key=api_key)

    # ── Step 1: list all skills ───────────────────────────────────────────────
    print("== skills.list() — collecting all skills ==")
    all_skills = []
    async for sk in client.beta.skills.list(limit=100):
        all_skills.append(sk)

    print(f"total skills in workspace: {len(all_skills)}")
    if not all_skills:
        print("WARNING: no skills found — workspace may be empty")
        return

    # ── Step 2: for each skill, call retrieve() and print full shape ──────────
    print()
    print("== skills.retrieve(skill_id) — full response shape per skill ==")
    print()

    has_content_fields = False
    content_field_names: set[str] = set()

    for i, sk in enumerate(all_skills, 1):
        print(f"  [{i}/{len(all_skills)}] id={sk.id!r}  source={sk.source!r}")
        if sk.source == "anthropic":
            print("    NOTE: this is an Anthropic built-in skill (not user-created)")

        try:
            retrieved = await client.beta.skills.retrieve(sk.id)
        except Exception as e:
            print(f"    ERROR on retrieve: {type(e).__name__}: {e}")
            continue

        print(f"    retrieve() response type: {type(retrieved).__name__}")
        print(f"    declared fields and values:")
        print(_shape_summary(retrieved))

        # Check specifically for content-bearing fields
        content_candidates = ["files", "content", "zip", "source_files", "body", "data"]
        for candidate in content_candidates:
            val = getattr(retrieved, candidate, None)
            if val is not None:
                has_content_fields = True
                content_field_names.add(candidate)
                print(f"    CONTENT FIELD FOUND: {candidate!r} = {type(val).__name__}")
        print()

    # ── Step 3: skills.create() signature summary (static, no call) ──────────
    print("== skills.create() accepted parameters (from SDK signature) ==")
    print("  display_title: str | None")
    print("  files: list[FileTypes] | None  (multipart upload — tuple of (filename, bytes/IO, content-type))")
    print("  (no 'content' or 'zip' parameter — create is file-upload only)")
    print()

    # ── Conclusion ────────────────────────────────────────────────────────────
    print("== CONCLUSION ==")
    if has_content_fields:
        print(f"  retrieve() DOES return content fields: {sorted(content_field_names)}")
        print("  ROUND-TRIP VERDICT: POSSIBLE — retrieve provides uploadable content.")
    else:
        print("  retrieve() returns NO content fields.")
        print(f"  Fields present: id, created_at, display_title, latest_version, source, type, updated_at")
        print("  ROUND-TRIP VERDICT: NOT POSSIBLE via retrieve() alone.")
        print("  skills fork MUST source zip content from local defaults/skills/ tree,")
        print("  not from MA. The retrieve response is metadata-only.")


if __name__ == "__main__":
    asyncio.run(main())
