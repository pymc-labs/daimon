"""Bundle a fetched tarball into per-skill zip-ready entries.

Architectural rule: NO try/except — exceptions propagate. The orchestrator
is the named boundary that catches them. Heavy file I/O (tarfile extract,
directory walk) is wrapped in asyncio.to_thread so it does not block the
event loop under the orchestrator's bounded-concurrency semaphore (Pitfall 5).
"""

from __future__ import annotations

import asyncio
import io
import tarfile
from dataclasses import dataclass
from pathlib import Path

import structlog
from daimon.core.skill_sync.fetcher import TarballTooLarge
from daimon.core.skill_zip import (
    RESERVED_SKILL_NAME_WORDS,
    build_bundled_zip,
    parse_skill_frontmatter,
    sanitize_skill_name,
)

_log = structlog.get_logger(__name__)

# Mirrors GithubSettings.max_tarball_decompressed_bytes' default (config.py) and
# the sibling pipeline's default (daimon.core.skills.fetch.fetch_repo), so an
# operator's configured cap guards this pipeline identically without any code
# change here when a caller threads the setting.
DEFAULT_MAX_TARBALL_DECOMPRESSED_BYTES: int = 200 * 1024 * 1024

# A tarball of millions of tiny files exhausts inodes and wall-clock even under
# a byte cap — each member still costs a stat/write syscall during extraction —
# so member count is bounded independently of total decompressed size.
MAX_TARBALL_MEMBERS: int = 10_000


@dataclass(frozen=True)
class SkillEntry:
    name: str
    skill_dir: Path
    source_rel: str
    prebuilt_zip: bytes | None
    skip_reason: str | None = None  # if set, orchestrator records as failed_upload


def _reserved_word_in_skill_md(skill_dir: Path) -> str | None:
    """If SKILL.md `name:` field contains a reserved word, return the word.

    MA validates SKILL.md frontmatter `name:` against reserved words
    (probed live 2026-05-09). The Python-side sanitize_skill_name only
    affects the upload-arg name, not the SKILL.md content inside the zip,
    so detection has to read the actual SKILL.md.
    """
    skill_md = skill_dir / "SKILL.md"
    if not skill_md.exists():
        # Try lowercase variant
        for child in skill_dir.iterdir():
            if child.name.lower() == "skill.md":
                skill_md = child
                break
        else:
            return None
    try:
        text = skill_md.read_text(encoding="utf-8", errors="replace")
        fields, _body = parse_skill_frontmatter(text)
    except Exception:  # malformed SKILL.md is not our problem here
        return None
    md_name = fields.get("name", "").lower()
    for word in RESERVED_SKILL_NAME_WORDS:
        if word in md_name:
            return word
    return None


def _smart_strip(extract_root: Path) -> Path:
    entries = list(extract_root.iterdir())
    dirs = [e for e in entries if e.is_dir()]
    has_root_skill = (extract_root / "SKILL.md").exists() or (extract_root / "skill.md").exists()
    if len(dirs) == 1 and not has_root_skill:
        return dirs[0]
    return extract_root


def _find_skill_md_dirs(root: Path) -> list[Path]:
    """Case-insensitive walk for SKILL.md. Returns parent directories,
    sorted lexicographically by path for determinism."""
    found: list[Path] = []
    for p in root.rglob("*"):
        if p.is_file() and p.name.lower() == "skill.md":
            found.append(p.parent)
    return sorted(found, key=lambda x: x.as_posix())


def _extract_and_bundle_sync(
    *,
    tarball_bytes: bytes,
    extract_root: Path,
    repo_name: str,
    split: bool,
    max_tarball_decompressed_bytes: int = DEFAULT_MAX_TARBALL_DECOMPRESSED_BYTES,
    max_tarball_members: int = MAX_TARBALL_MEMBERS,
) -> list[SkillEntry]:
    with tarfile.open(fileobj=io.BytesIO(tarball_bytes), mode="r:gz") as tf:
        # Pre-extraction accounting: bound expansion (a small tarball can
        # decompress to tens of gigabytes) and member count, BEFORE any file is
        # written. A cap of 0 disables that check, matching max_tarball_bytes'
        # existing convention on the compressed download side.
        members = tf.getmembers()
        if max_tarball_members > 0 and len(members) > max_tarball_members:
            raise TarballTooLarge(
                f"tarball for {repo_name!r} contains {len(members)} members, exceeding "
                f"max_tarball_members cap of {max_tarball_members}"
            )
        total_decompressed_bytes = sum(m.size for m in members)
        if (
            max_tarball_decompressed_bytes > 0
            and total_decompressed_bytes > max_tarball_decompressed_bytes
        ):
            raise TarballTooLarge(
                f"tarball for {repo_name!r} decompresses to {total_decompressed_bytes} bytes, "
                f"exceeding max_tarball_decompressed_bytes cap of "
                f"{max_tarball_decompressed_bytes} bytes"
            )
        # CVE-2007-4559 safe extraction: filter="data" blocks path traversal,
        # symlinks escaping the root, and device/special files. It does NOT
        # bound expansion size or member count — the accounting above covers
        # that separately, before any of these files are written.
        tf.extractall(extract_root, filter="data")

    repo_root = _smart_strip(extract_root)

    if not split:
        zip_bytes, _arcname_prefix, _included = build_bundled_zip(repo_root, repo_name)
        return [
            SkillEntry(
                name=sanitize_skill_name(repo_name),
                skill_dir=repo_root,
                source_rel="",
                prebuilt_zip=zip_bytes,
            )
        ]

    skill_dirs = _find_skill_md_dirs(repo_root)
    entries: list[SkillEntry] = []
    seen_names: set[str] = set()
    for skill_dir in skill_dirs:
        # MA enforces that the zip's arcname_prefix matches the SKILL.md
        # `name:` frontmatter field; rejecting otherwise with
        # `folder name 'X' must match the skill name 'Y' in SKILL.md`.
        # Prefer the SKILL.md `name:` so the zip's prefix and the SKILL.md
        # inside agree by construction. Fall back to the folder name when
        # the frontmatter has no `name:` (e.g. synthesized manifests).
        manifest_name: str | None = None
        for candidate in ("SKILL.md", "skill.md"):
            md = skill_dir / candidate
            if md.is_file():
                try:
                    fields, _body = parse_skill_frontmatter(
                        md.read_text(encoding="utf-8", errors="replace")
                    )
                    manifest_name = fields.get("name") or None
                except Exception:  # malformed frontmatter is not fatal here
                    manifest_name = None
                break
        if manifest_name is not None:
            raw_name = manifest_name
        else:
            raw_name = skill_dir.name if skill_dir != repo_root else repo_name
        name = sanitize_skill_name(raw_name)
        if name in seen_names:
            _log.warning(
                "skill_sync.bundler.duplicate_name_in_repo",
                repo=repo_name,
                name=name,
            )
            continue
        seen_names.add(name)
        source_rel = skill_dir.relative_to(repo_root).as_posix() if skill_dir != repo_root else ""

        reserved = _reserved_word_in_skill_md(skill_dir)
        if reserved is not None:
            _log.warning(
                "skill_sync.bundler.reserved_word_in_skill_name",
                repo=repo_name,
                name=name,
                reserved_word=reserved,
            )
            entries.append(
                SkillEntry(
                    name=name,
                    skill_dir=skill_dir,
                    source_rel=source_rel,
                    prebuilt_zip=None,
                    skip_reason=(
                        f"SKILL.md name field contains MA-reserved word "
                        f"{reserved!r}; rename in source repo"
                    ),
                )
            )
            continue

        entries.append(
            SkillEntry(
                name=name,
                skill_dir=skill_dir,
                source_rel=source_rel,
                prebuilt_zip=None,
            )
        )
    return entries


async def extract_and_bundle(
    *,
    tarball_bytes: bytes,
    extract_root: Path,
    repo_name: str,
    split: bool,
    max_tarball_decompressed_bytes: int = DEFAULT_MAX_TARBALL_DECOMPRESSED_BYTES,
    max_tarball_members: int = MAX_TARBALL_MEMBERS,
) -> list[SkillEntry]:
    return await asyncio.to_thread(
        _extract_and_bundle_sync,
        tarball_bytes=tarball_bytes,
        extract_root=extract_root,
        repo_name=repo_name,
        split=split,
        max_tarball_decompressed_bytes=max_tarball_decompressed_bytes,
        max_tarball_members=max_tarball_members,
    )
