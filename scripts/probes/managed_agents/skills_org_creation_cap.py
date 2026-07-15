"""Probe: does the MA org have a skill-creation cap?

Question: Does skills.create fail with a non-429 4xx after some number of skills
are created on the org?  The per-agent pin cap (400 "exceeds maximum" on
agents.update) is known; this probe checks whether an org-level cap on skill
creation exists and, if so, at what count.

Method:
  - Create minimal zip skills titled daimon-probe-cap-{i:04d} in batches.
  - APPEND every created skill id to a local file immediately on creation,
    before the next create — so cleanup survives a crash (see risk note).
  - Stop at the first non-429 4xx (a new error shape = the cap; 429 = rate
    limit, not a cap signal — retry or abort with a message).
  - Hard bound: stop at MAX_CREATIONS; if no cap is found by then the verdict
    is NO_CAP_BELOW_{MAX_CREATIONS}.
  - finally: delete every recorded id via delete_skill_and_versions; print any
    deletion failures with ids for manual cleanup; remove the id file only when
    all deletions succeeded.

Risk note:
  A partial-cleanup crash leaves probe skills counted against the org's
  100-visible window (skills.list pagination is broken — only the first 100
  skills are visible via LIST, but there is no list cap on the org count itself).
  Recovery: delete by recorded id using delete_skill_and_versions — this works
  regardless of list visibility because it operates on the skill id directly.
  The id file (/tmp/daimon_probe_cap_ids.txt) survives crashes for this reason.

OPERATOR-GATED: Do NOT run this probe without human approval.  It creates up to
MAX_CREATIONS (250) live skills on the operator's shared MA org and deletes them.
All tenants share the same org; a crash mid-run temporarily reduces the org's
100-visible window.

Inputs (env): ANTHROPIC_API_KEY or DAIMON_ANTHROPIC__API_KEY.
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

# Hard bound: stop after this many creations regardless of errors.
# 250 = enough for 100 guilds × 3 seeded skills (300) with some headroom to
# prove no cap before the product ceiling; staying under 300 leaves slack.
MAX_CREATIONS = 250

# Batch size for creates; larger batches run faster but lose more to a crash.
BATCH_SIZE = 10

# Local file tracking every created id — append-on-create so a crash does not
# lose ids that were created before the crash.
ID_FILE = "/tmp/daimon_probe_cap_ids.txt"

SKILL_MD_CONTENT = """\
---
name: probe
description: daimon org-cap probe skill
---
probe body
"""


def _make_skill_zip() -> bytes:
    """Build a minimal skill zip in memory: probe/SKILL.md."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("probe/SKILL.md", SKILL_MD_CONTENT)
    return buf.getvalue()


def _append_id(skill_id: str) -> None:
    """Append a created skill id to the id file (crash-safe record)."""
    with open(ID_FILE, "a") as f:
        f.write(skill_id + "\n")


def _load_ids() -> list[str]:
    """Load all recorded ids from the id file.  Returns [] if file absent."""
    try:
        with open(ID_FILE) as f:
            return [line.strip() for line in f if line.strip()]
    except FileNotFoundError:
        return []


async def _delete_all_recorded(client: AsyncAnthropic) -> bool:
    """Delete every id in the id file.  Returns True if all succeeded."""
    # Import here so the probe is self-contained without daimon.core on sys.path.
    # The probe is in scripts/ (not a package member); add core to the path so
    # delete_skill_and_versions is available without requiring the operator to
    # install daimon-core as a package.
    import importlib.util
    import pathlib

    repo_root = pathlib.Path(__file__).parent.parent.parent.parent
    core_src = repo_root / "packages" / "core"
    spec = importlib.util.spec_from_file_location(
        "daimon.core.ma",
        core_src / "daimon" / "core" / "ma.py",
    )
    if spec is None or spec.loader is None:
        print(
            "  ERROR: could not locate daimon.core.ma — "
            "delete ids manually from the id file.",
            file=sys.stderr,
        )
        return False

    # The probe runs via `uv run python` from the repo root; the workspace
    # venv has daimon-core installed as an editable dep, so a normal import
    # works.  The importlib path above is a fallback; use the normal import.
    try:
        from daimon.core.ma import delete_skill_and_versions
    except ImportError:
        # Fallback: run the importlib path (for environments where the package
        # is not on sys.path but the source tree is present).
        ma_mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(ma_mod)  # type: ignore[union-attr]
        delete_skill_and_versions = ma_mod.delete_skill_and_versions  # type: ignore[attr-defined]

    ids = _load_ids()
    if not ids:
        print("  (no ids recorded — nothing to delete)")
        return True

    print(f"  deleting {len(ids)} recorded skills…")
    failed_ids: list[str] = []
    for skill_id in ids:
        try:
            await delete_skill_and_versions(client, skill_id)
            print(f"    deleted {skill_id!r}")
        except anthropic.APIError as err:
            print(f"    FAILED to delete {skill_id!r}: {err}")
            failed_ids.append(skill_id)

    if failed_ids:
        print(
            f"  WARNING: {len(failed_ids)} skills could not be deleted.\n"
            "  Manual cleanup required — delete these ids via API or dashboard:\n"
            + "\n".join(f"    {sid}" for sid in failed_ids)
        )
        return False

    print(f"  all {len(ids)} skills deleted")
    return True


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
    created_count = 0
    no_cap_msg = (
        f"NO_CAP_BELOW_{MAX_CREATIONS} — "
        "sufficient for 100 guilds x 3 seeded skills + headroom"
    )
    cap_verdict = no_cap_msg

    # Clear the id file at the start of a fresh run (not a resume).
    # On resume (id file exists from a prior crash), operator should have
    # manually deleted the recorded ids before re-running.
    if os.path.exists(ID_FILE):
        print(
            f"WARNING: {ID_FILE} already exists from a previous run.\n"
            "If you are resuming after a crash, delete the recorded ids first "
            "by running this probe's _delete_all_recorded() helper directly, "
            "then remove the id file and re-run.",
            file=sys.stderr,
        )
        return 2

    print(f"== Org skill-creation cap probe (max={MAX_CREATIONS}, batch={BATCH_SIZE}) ==")
    print(f"  id file: {ID_FILE}")
    print()

    try:
        while created_count < MAX_CREATIONS:
            batch_end = min(created_count + BATCH_SIZE, MAX_CREATIONS)
            print(
                f"  batch {created_count + 1}–{batch_end} "
                f"(total so far: {created_count}) …"
            )
            for i in range(created_count, batch_end):
                try:
                    skill = await client.beta.skills.create(
                        display_title=f"daimon-probe-cap-{i:04d}",
                        files=[
                            ("SKILL.zip", io.BytesIO(skill_zip), "application/zip")
                        ],
                    )
                    # APPEND id immediately before the next create — crash-safe.
                    _append_id(skill.id)
                    created_count += 1
                except anthropic.APIStatusError as err:
                    if err.status_code == 429:
                        # Rate limit — not a cap signal; abort with message.
                        print(
                            f"  429 rate-limit at creation {created_count}: {err.message!r}"
                        )
                        print(
                            "  Re-run after the rate-limit window resets.  "
                            "Recorded ids will be cleaned up in finally."
                        )
                        cap_verdict = f"RATE_LIMITED at creation {created_count} — not a cap signal"
                        return 1
                    else:
                        # Non-429 4xx or 5xx — likely the cap.
                        print(
                            f"  STOP: non-429 error at creation {created_count}:\n"
                            f"    status_code={err.status_code}\n"
                            f"    message={err.message!r}"
                        )
                        cap_verdict = (
                            f"CAP_FOUND at {created_count} creations: "
                            f"{err.status_code} — {err.message!r}"
                        )
                        return 0

            print(f"  created {created_count} skills so far — no cap yet")

        # Reached MAX_CREATIONS without hitting a cap.
        print(
            f"\n  Reached {MAX_CREATIONS} creations without a non-429 4xx error."
        )

    finally:
        print(f"\n== Cleanup (deleting {created_count} probe skills) ==")
        all_deleted = await _delete_all_recorded(client)
        if all_deleted and os.path.exists(ID_FILE):
            os.remove(ID_FILE)
            print(f"  removed id file {ID_FILE}")
        print()
        print(f"VERDICT: {cap_verdict}")

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
