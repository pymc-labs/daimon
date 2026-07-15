"""Probe: characterize the APIStatusError raised on duplicate skill display_title.

The companion probe ``skills_display_title_uniqueness.py`` answers *whether*
display_title is enforced as unique. This probe answers *what the error looks
like* when it is enforced — ``type(err).__name__``, ``err.status_code``,
``err.message``, ``err.body`` — so a duplicate-title recovery branch in the
seed orchestrator can match the SDK's error contract precisely (status-code
branch vs. message-substring fallback).

DESTRUCTIVE WARNING: Creates real skill resources on your Anthropic workspace
and deletes them on exit (best-effort). A mid-run crash could leave orphans
named with display_title=``daimon-probe dup-title-test``; delete via API or
dashboard if so.

Inputs (env): ANTHROPIC_API_KEY or DAIMON_ANTHROPIC__API_KEY.

Re-run any time the ``anthropic`` SDK upgrades to re-confirm the contract.
"""

from __future__ import annotations

import asyncio
import io
import os
import sys
import zipfile

import anthropic
from anthropic import AsyncAnthropic
from dotenv import load_dotenv

DUP_TITLE = "daimon-probe dup-title-test"

SKILL_MD_CONTENT = """\
---
name: probe
description: probe
---
probe body
"""


def _make_skill_zip() -> bytes:
    """Build a minimal skill zip in memory: probe/SKILL.md."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("probe/SKILL.md", SKILL_MD_CONTENT)
    return buf.getvalue()


async def _delete_skill(client: AsyncAnthropic, skill_id: str) -> None:
    """Delete versions then the skill itself. Best-effort, prints on failure."""
    try:
        async for ver in client.beta.skills.versions.list(skill_id, limit=100):
            try:
                await client.beta.skills.versions.delete(
                    ver.version, skill_id=skill_id
                )
                print(f"  deleted version {ver.version!r} from {skill_id!r}")
            except anthropic.APIError as err:
                print(
                    f"  WARNING: version delete failed for {skill_id!r}: "
                    f"{type(err).__name__}: {err}"
                )
        await client.beta.skills.delete(skill_id)
        print(f"  deleted skill {skill_id!r}")
    except anthropic.APIError as err:
        print(
            f"  WARNING: skill delete failed for {skill_id!r}: "
            f"{type(err).__name__}: {err}"
        )


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
    skill_zip = _make_skill_zip()
    first_id: str | None = None
    second_id: str | None = None

    try:
        print(f"== Step 1: create first skill display_title={DUP_TITLE!r} ==")
        first = await client.beta.skills.create(
            display_title=DUP_TITLE,
            files=[("SKILL.zip", io.BytesIO(skill_zip), "application/zip")],
        )
        first_id = first.id
        print(f"  ok id={first.id!r} latest_version={first.latest_version!r}")
        print()

        print(
            f"== Step 2: create second skill with SAME display_title={DUP_TITLE!r} =="
        )
        try:
            second = await client.beta.skills.create(
                display_title=DUP_TITLE,
                files=[("SKILL.zip", io.BytesIO(skill_zip), "application/zip")],
            )
            second_id = second.id
            print(
                f"  UNEXPECTED success id={second.id!r} — "
                "display_title is NOT globally unique on this workspace"
            )
            print(
                "  (See skills_display_title_uniqueness.py — collision risk path.)"
            )
            return 1
        except anthropic.APIStatusError as err:
            print("== DUPLICATE-TITLE ERROR CHARACTERIZATION ==")
            print(f"  exception type:   {type(err).__name__}")
            print(f"  module:           {type(err).__module__}")
            print(f"  status_code:      {err.status_code}")
            print(f"  message:          {err.message!r}")
            print(f"  body:             {err.body!r}")
            print(f"  response.headers: {dict(err.response.headers)}")
            print()
            print(
                "  Wave-3 orchestrator should branch on status_code if it is a "
                "stable 4xx; otherwise fall back to substring-match on message."
            )
            return 0
    finally:
        print()
        print("== Cleanup ==")
        for sid in (first_id, second_id):
            if sid is not None:
                await _delete_skill(client, sid)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
