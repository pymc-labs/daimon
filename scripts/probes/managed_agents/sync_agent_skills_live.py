"""Live UAT for Phase 33 sync pipeline against real GitHub + real MA.

Self-contained. No DB needed — calls the orchestrator's sub-steps directly:
  fetcher.fetch_tarball → bundler.extract_and_bundle → skills.create.

Two rounds:
  Round 1 — fresh upload. Mix of well-formed repos + 404s + a no-SKILL repo.
  Round 2 — same display_titles → expect MA to reject with the duplicate-title
            400 (the shape we probed in commit 4a7e302), then recover via
            find_skill_by_display_title + versions.create.

Cleanup (default on): deletes every skill it created.

Setup it does itself:
  - Loads ANTHROPIC_API_KEY + GITHUB_TOKEN from the env file at
    DAIMON_PROBE_ENV_FILE (defaults to .env in the cwd).

Run:
    uv run python scripts/probes/managed_agents/sync_agent_skills_live.py
    uv run python scripts/probes/managed_agents/sync_agent_skills_live.py --no-cleanup
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import io
import os
import sys
import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import anthropic
import httpx
from anthropic import AsyncAnthropic

from daimon.core.defaults.ma_index import find_skill_by_display_title
from daimon.core.skill_sync.bundler import SkillEntry, extract_and_bundle
from daimon.core.skill_sync.fetcher import (
    GitHubAuthError,
    GitHubTarballFetcher,
    GitHubUnreachable,
)
from daimon.core.skill_sync.orchestrator import _looks_like_duplicate_title
from daimon.core.skill_zip import canonical_zip_bytes
from daimon.core.specs import SkillRepo


def _load_env() -> tuple[str, str]:
    env_path = Path(os.environ.get("DAIMON_PROBE_ENV_FILE", ".env"))
    if not env_path.is_file():
        raise SystemExit(
            f"env file not found: {env_path} "
            "(set DAIMON_PROBE_ENV_FILE to point at a file with "
            "ANTHROPIC_API_KEY and GITHUB_TOKEN)"
        )
    env: dict[str, str] = {}
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        env[k.strip()] = v.strip().strip('"').strip("'")
    a = env.get("ANTHROPIC_API_KEY")
    g = env.get("GITHUB_TOKEN")
    if not a:
        raise RuntimeError(f"ANTHROPIC_API_KEY missing in {env_path}")
    if not g:
        raise RuntimeError(f"GITHUB_TOKEN missing in {env_path}")
    return a, g


REPOS: list[tuple[SkillRepo, str]] = [
    (
        SkillRepo(url="anthropics/skills", branch="main", path="", split=True),
        "well-formed (split): expect ~17 skills uploaded",
    ),
    (
        SkillRepo(url="anthropics/skills", branch="main", path="", split=False),
        "no-SKILL-at-root: expect MA to reject the bundled zip",
    ),
    (
        SkillRepo(
            url="anthropics/this-repo-does-not-exist-asdf-zzz",
            branch="main",
            path="",
            split=False,
        ),
        "404: nonexistent repo under valid org",
    ),
    (
        SkillRepo(
            url="totally-fake-org-xyz-9999/whatever",
            branch="main",
            path="",
            split=False,
        ),
        "404: nonexistent org+repo",
    ),
    (
        SkillRepo(
            url="anthropics/skills",
            branch="this-branch-does-not-exist-zzz",
            path="",
            split=True,
        ),
        "404: real repo, fake branch",
    ),
]

PRINCIPAL_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")  # unused since fix
AGENT_NAME = "phase33-spike"


def _format_display_title(name: str) -> str:
    # Mirrors orchestrator._format_display_title (post-fix: no UUID prefix).
    return f"{AGENT_NAME}/{name}"


@dataclass
class RoundOutcome:
    fetched: list[str] = field(default_factory=list)
    skipped_repos: list[tuple[str, str]] = field(default_factory=list)
    bundled_entries: list[tuple[str, str]] = field(default_factory=list)  # (repo, name)
    created: list[tuple[str, str, str]] = field(default_factory=list)  # (name, id, content_hash)
    versioned: list[tuple[str, str]] = field(default_factory=list)  # (name, version)
    failed_uploads: list[tuple[str, str]] = field(default_factory=list)
    dup_title_recoveries: int = 0


async def _do_round(
    *,
    label: str,
    repos: list[SkillRepo],
    fetcher: GitHubTarballFetcher,
    pat: str,
    anthropic_client: AsyncAnthropic,
    known_skill_ids: dict[str, str],  # display_title → skill_id (for round 2)
    known_hashes: dict[str, str],  # name → content_hash (for dedup)
) -> RoundOutcome:
    out = RoundOutcome()
    print(f"\n========= {label} =========")

    with tempfile.TemporaryDirectory(prefix="spike-") as tmp_root:
        tmp_root_path = Path(tmp_root)
        # Phase 1: fetch + bundle
        all_entries: list[tuple[str, SkillEntry]] = []
        for repo in repos:
            try:
                tarball = await fetcher.fetch_tarball(
                    pat=pat, url=repo.url, branch=repo.branch
                )
                out.fetched.append(repo.url)
                print(f"  FETCH ok: {repo.url}@{repo.branch} ({len(tarball):,} bytes)")
            except (GitHubAuthError, GitHubUnreachable) as err:
                out.skipped_repos.append((repo.url, type(err).__name__))
                print(f"  FETCH skip: {repo.url}@{repo.branch} ({type(err).__name__})")
                continue
            except Exception as err:  # noqa: BLE001
                out.skipped_repos.append((repo.url, f"{type(err).__name__}: {err}"))
                print(f"  FETCH error: {repo.url}: {err}")
                continue

            extract_root = tmp_root_path / hashlib.sha256(
                f"{repo.url}@{repo.branch}".encode()
            ).hexdigest()
            extract_root.mkdir(parents=True, exist_ok=True)
            repo_name = repo.url.rstrip("/").rsplit("/", 1)[-1].removesuffix(".git")

            try:
                entries = await extract_and_bundle(
                    tarball_bytes=tarball,
                    extract_root=extract_root,
                    repo_name=repo_name,
                    split=repo.split,
                )
                for entry in entries:
                    all_entries.append((repo.url, entry))
                    out.bundled_entries.append((repo.url, entry.name))
                print(f"  BUNDLE: {repo.url} → {len(entries)} entries")
            except Exception as err:  # noqa: BLE001
                out.skipped_repos.append((repo.url, f"bundle: {err}"))
                print(f"  BUNDLE error: {repo.url}: {err}")

        # Phase 2: per-entry upload (zip + MA call)
        sem = asyncio.Semaphore(6)

        async def _upload_one(repo_url: str, entry: SkillEntry) -> None:
            async with sem:
                if entry.skip_reason is not None:
                    out.failed_uploads.append((entry.name, entry.skip_reason))
                    print(f"  SKIP (bundler): {entry.name} — {entry.skip_reason}")
                    return
                if entry.prebuilt_zip is not None:
                    zip_bytes = entry.prebuilt_zip
                else:
                    zip_bytes = await asyncio.to_thread(
                        canonical_zip_bytes, entry.skill_dir, arcname_prefix=entry.name
                    )
                content_hash = hashlib.sha256(zip_bytes).hexdigest()
                display_title = _format_display_title(entry.name)

                # Dedup short-circuit (in-memory analog of DB-backed dedup)
                if known_hashes.get(entry.name) == content_hash:
                    print(f"  DEDUP skip: {entry.name} (hash unchanged)")
                    return

                try:
                    if entry.name in known_hashes and entry.name in [
                        n for dt, n in [(_format_display_title(k), k) for k in known_hashes]
                    ]:
                        # Round 2 path with hash change: version-create
                        skill_id = known_skill_ids[display_title]
                        resp = await asyncio.wait_for(
                            anthropic_client.beta.skills.versions.create(
                                skill_id=skill_id,
                                files=[("SKILL.zip", io.BytesIO(zip_bytes), "application/zip")],
                            ),
                            timeout=60.0,
                        )
                        out.versioned.append((entry.name, resp.version))
                        known_hashes[entry.name] = content_hash
                        print(f"  VERSION: {entry.name} → v{resp.version}")
                        return

                    created = await asyncio.wait_for(
                        anthropic_client.beta.skills.create(
                            display_title=display_title,
                            files=[("SKILL.zip", io.BytesIO(zip_bytes), "application/zip")],
                        ),
                        timeout=60.0,
                    )
                    out.created.append((entry.name, created.id, content_hash))
                    known_skill_ids[display_title] = created.id
                    known_hashes[entry.name] = content_hash
                    print(f"  CREATE: {entry.name} → {created.id}")
                except anthropic.APIStatusError as err:
                    if not _looks_like_duplicate_title(err):
                        out.failed_uploads.append(
                            (entry.name, f"{err.status_code}: {err.message[:120]}")
                        )
                        print(
                            f"  CREATE fail: {entry.name} → "
                            f"{err.status_code}: {err.message[:120]}"
                        )
                        return
                    # Recovery branch — exactly what the orchestrator does
                    out.dup_title_recoveries += 1
                    recovered = await find_skill_by_display_title(
                        anthropic_client, display_title, on_truncation="degrade"
                    )
                    if recovered is None:
                        out.failed_uploads.append(
                            (entry.name, "dup-title but find returned None")
                        )
                        print(f"  RECOVERY fail: {entry.name} (find returned None)")
                        return
                    resp = await anthropic_client.beta.skills.versions.create(
                        skill_id=recovered.id,
                        files=[("SKILL.zip", io.BytesIO(zip_bytes), "application/zip")],
                    )
                    out.versioned.append((entry.name, resp.version))
                    known_skill_ids[display_title] = recovered.id
                    known_hashes[entry.name] = content_hash
                    print(
                        f"  RECOVERY: {entry.name} → "
                        f"existing {recovered.id} → v{resp.version}"
                    )
                except Exception as err:  # noqa: BLE001
                    out.failed_uploads.append((entry.name, f"{type(err).__name__}: {err}"))
                    print(f"  UPLOAD error: {entry.name}: {err}")

        # Cap total entries at 20 to honor the user's "20 skills" framing
        capped = all_entries[:20]
        if len(all_entries) > 20:
            print(f"  (capping {len(all_entries)} → 20 entries)")
        await asyncio.gather(*[_upload_one(u, e) for u, e in capped])

    return out


async def _print_summary(round_label: str, out: RoundOutcome) -> None:
    print(f"\n--- {round_label} summary ---")
    print(f"  repos fetched:      {len(out.fetched)}")
    print(f"  repos skipped:      {len(out.skipped_repos)}")
    for u, r in out.skipped_repos:
        print(f"    - {u}: {r}")
    print(f"  bundle entries:     {len(out.bundled_entries)}")
    print(f"  MA created:         {len(out.created)}")
    print(f"  MA versioned:       {len(out.versioned)}")
    print(f"  dup-title recovers: {out.dup_title_recoveries}")
    print(f"  upload failures:    {len(out.failed_uploads)}")
    for n, r in out.failed_uploads:
        print(f"    - {n}: {r}")


async def _cleanup(
    anthropic_client: AsyncAnthropic, skill_ids: dict[str, str]
) -> None:
    print("\n========= CLEANUP =========")
    deleted = 0
    failed = 0
    for display_title, skill_id in skill_ids.items():
        try:
            # MA refuses skill.delete while versions exist; drop versions first.
            async for v in anthropic_client.beta.skills.versions.list(skill_id=skill_id):
                try:
                    await anthropic_client.beta.skills.versions.delete(
                        skill_id=skill_id, version=v.version
                    )
                except Exception as v_err:  # noqa: BLE001
                    print(f"  version-delete fail: {display_title}/{v.version}: {v_err}")
            await anthropic_client.beta.skills.delete(skill_id=skill_id)
            deleted += 1
        except Exception as err:  # noqa: BLE001
            failed += 1
            print(f"  delete fail: {display_title} ({skill_id}): {err}")
    print(f"  deleted: {deleted}, failed: {failed}")


async def main(no_cleanup: bool) -> int:
    anthropic_key, gh_token = _load_env()

    print("Repos under test:")
    for r, label in REPOS:
        print(f"  {r.url}@{r.branch} split={r.split} — {label}")

    repos = [r for r, _ in REPOS]
    known_skill_ids: dict[str, str] = {}
    known_hashes: dict[str, str] = {}

    async with httpx.AsyncClient(timeout=httpx.Timeout(60.0)) as http_client:
        anthropic_client = AsyncAnthropic(api_key=anthropic_key)
        fetcher = GitHubTarballFetcher(http_client)

        try:
            r1 = await _do_round(
                label="ROUND 1 (fresh upload)",
                repos=repos,
                fetcher=fetcher,
                pat=gh_token,
                anthropic_client=anthropic_client,
                known_skill_ids=known_skill_ids,
                known_hashes={},  # fresh — no dedup
            )
            await _print_summary("ROUND 1", r1)

            print("\n--- Sleeping 2s before round 2 ---")
            await asyncio.sleep(2)

            # Round 2: simulate the orchestrator's "DB lost, retry from scratch" path.
            # We pass empty known_hashes so it tries to CREATE again → expect dup-title 400 → recovery.
            r2 = await _do_round(
                label="ROUND 2 (re-run, expect dup-title recovery via versions.create)",
                repos=repos,
                fetcher=fetcher,
                pat=gh_token,
                anthropic_client=anthropic_client,
                known_skill_ids=known_skill_ids,
                known_hashes={},  # force re-create attempt
            )
            await _print_summary("ROUND 2", r2)

            print("\n========= ASSERTIONS =========")
            ok = True
            if r1.skipped_repos and len(r1.skipped_repos) < 3:
                print(
                    f"  WARN: round 1 expected 3 skipped repos (3x404), got "
                    f"{len(r1.skipped_repos)}"
                )
            if not r1.created:
                print("  FAIL: round 1 created no skills")
                ok = False
            else:
                print(f"  PASS: round 1 created {len(r1.created)} skills")

            if r2.dup_title_recoveries == 0:
                print("  FAIL: round 2 should have hit dup-title recovery for every existing skill")
                ok = False
            else:
                print(
                    f"  PASS: round 2 recovered {r2.dup_title_recoveries} via "
                    f"find_skill_by_display_title + versions.create"
                )

            if r2.created:
                print(
                    f"  WARN: round 2 created {len(r2.created)} new skills "
                    f"(unexpected unless round 1 partially failed)"
                )

            if ok:
                print("\n  OVERALL: PASS")
            else:
                print("\n  OVERALL: FAIL")
        finally:
            if not no_cleanup:
                await _cleanup(anthropic_client, known_skill_ids)

    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-cleanup", action="store_true")
    args = parser.parse_args()
    sys.exit(asyncio.run(main(no_cleanup=args.no_cleanup)))
