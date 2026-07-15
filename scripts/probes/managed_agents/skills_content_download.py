"""Probe: can skill content be downloaded with API-key credentials?

2026-06-10 verdict: NO.

The endpoint GET /v1/skills/{id}/versions/{ver}/content exists (with
anthropic-beta: skills-2025-10-02 header) but returns 403 permission_error:
"Downloading skill content is not supported with this credential type."
The ?include=files query parameter is silently ignored (byte-identical response
to a plain versions.retrieve call).

Implication for D-06/D-07 (backfill content source):
  - Seeded skills: rebuild from defaults/ tree via build_skill_zip(skill_dir).
  - Synced skills: re-fetch from source repo (user_skills.source_repo_url).
  - Skill content is not recoverable from the MA API with API-key auth.

Re-run this probe after any anthropic SDK or beta-version upgrade.  If the
verdict changes to CONTENT_DOWNLOAD: AVAILABLE, the D-06 backfill content
source can switch to MA download.

Read-only: this probe creates zero MA resources.

Inputs (env): ANTHROPIC_API_KEY or DAIMON_ANTHROPIC__API_KEY.
"""

from __future__ import annotations

import asyncio
import os
import sys

import anthropic
from anthropic import AsyncAnthropic
from dotenv import load_dotenv


async def main() -> int:
    load_dotenv()
    api_key = os.environ.get("ANTHROPIC_API_KEY") or os.environ.get(
        "DAIMON_ANTHROPIC__API_KEY"
    )
    if not api_key:
        print(
            "Set ANTHROPIC_API_KEY or DAIMON_ANTHROPIC__API_KEY",
            file=sys.stderr,
        )
        return 2

    client = AsyncAnthropic(api_key=api_key)

    # ── Find the first existing custom skill ──────────────────────────────────
    print("== Finding first custom skill on org ==")
    skill_id: str | None = None
    async for sk in client.beta.skills.list(limit=10):
        if sk.source == "custom":
            skill_id = sk.id
            print(
                f"  found skill id={sk.id!r} display_title={sk.display_title!r} "
                f"latest_version={sk.latest_version!r}"
            )
            break

    if skill_id is None:
        print(
            "  SKIP: no custom skills found on org — "
            "create at least one custom skill and re-run."
        )
        return 0

    # ── Step 1: skills.retrieve (metadata only) ───────────────────────────────
    print("\n== Step 1: skills.retrieve(id) — metadata fields ==")
    skill = await client.beta.skills.retrieve(skill_id)
    field_names = sorted(skill.model_fields_set)
    print(f"  field names: {field_names}")
    print(f"  model_extra: {skill.model_extra}")

    # ── Step 2: versions.retrieve (should have no files field) ───────────────
    latest_version = skill.latest_version
    print(f"\n== Step 2: versions.retrieve(version={latest_version!r}) ==")
    ver = await client.beta.skills.versions.retrieve(
        latest_version, skill_id=skill_id
    )
    ver_field_names = sorted(ver.model_fields_set)
    print(f"  field names: {ver_field_names}")
    print(f"  model_extra: {ver.model_extra}")
    has_files_field = "files" in ver_field_names or "files" in (ver.model_extra or {})
    print(f"  has files field: {has_files_field}")

    # ── Step 3: raw GET /content endpoint ─────────────────────────────────────
    print(
        f"\n== Step 3: GET /v1/skills/{skill_id}/versions/{latest_version}/content =="
    )
    content_verdict = "CONTENT_DOWNLOAD: UNKNOWN"
    try:
        # Use the SDK's underlying HTTP client to make the raw request.
        # The /content endpoint requires the skills beta header.
        response = await client.get(
            f"/v1/skills/{skill_id}/versions/{latest_version}/content",
            options={
                "headers": {"anthropic-beta": "skills-2025-10-02"},
            },
            cast_to=object,
        )
        print(f"  UNEXPECTED 200 response: {response!r}")
        content_verdict = (
            "CONTENT_DOWNLOAD: AVAILABLE — "
            "D-06 backfill content source can switch to MA download"
        )
    except anthropic.APIStatusError as err:
        print(f"  status_code: {err.status_code}")
        print(f"  error type:  {err.type if hasattr(err, 'type') else 'N/A'}")  # type: ignore[attr-defined]
        print(f"  message:     {err.message!r}")
        if err.status_code == 403:
            content_verdict = "CONTENT_DOWNLOAD: BLOCKED (403 permission_error)"
        else:
            content_verdict = (
                f"CONTENT_DOWNLOAD: BLOCKED (unexpected {err.status_code})"
            )
    except anthropic.APIConnectionError as err:
        print(f"  connection error: {err}")
        content_verdict = "CONTENT_DOWNLOAD: CONNECTION_ERROR"

    # ── Step 4: ?include=files retrieve (should be byte-identical) ───────────
    print(
        f"\n== Step 4: versions.retrieve(version={latest_version!r}, include=['files']) =="
    )
    include_verdict = "INCLUDE_FILES: UNKNOWN"
    try:
        # The SDK doesn't have a typed include= param; use extra_query.
        ver_with_files = await client.beta.skills.versions.retrieve(
            latest_version,
            skill_id=skill_id,
            extra_query={"include": "files"},
        )
        include_field_names = sorted(ver_with_files.model_fields_set)
        include_extra = ver_with_files.model_extra
        same_as_plain = (
            include_field_names == ver_field_names
            and include_extra == ver.model_extra
        )
        print(f"  field names: {include_field_names}")
        print(f"  model_extra: {include_extra}")
        print(f"  byte-identical to plain retrieve: {same_as_plain}")
        if same_as_plain:
            include_verdict = "INCLUDE_FILES: PARAM_IGNORED (byte-identical to plain retrieve)"
        else:
            new_fields = set(include_field_names) - set(ver_field_names)
            include_verdict = f"INCLUDE_FILES: DIFFERS — new fields: {new_fields}"
    except anthropic.APIStatusError as err:
        print(f"  status_code: {err.status_code}  message: {err.message!r}")
        include_verdict = f"INCLUDE_FILES: ERROR ({err.status_code})"

    # ── Verdicts ──────────────────────────────────────────────────────────────
    print()
    print(f"VERDICT: {content_verdict}")
    print(f"VERDICT: {include_verdict}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
