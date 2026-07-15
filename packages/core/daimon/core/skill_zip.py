"""Deterministic zip builder for skill directories.

Two builders live here:

- :func:`build_skill_zip` — seed-time builder used by ``defaults/reconcile_skills``
  and ``skills/sync``. Writes to a temp file, drops unsafe paths, renames nested
  ``SKILL.md`` to ``_SKILL.md.txt`` so MA's resolver only sees the root manifest.
- :func:`canonical_zip_bytes` / :func:`build_bundled_zip` — sync-time builders
  that emit byte-deterministic archives in memory. Subtrees rooted at a nested
  ``SKILL.md`` are excluded entirely (they are their own skill).

Both share the same MA-imposed file-count and uncompressed-byte caps.

Frontmatter helpers (``has_frontmatter``, ``parse_skill_frontmatter``,
``wrap_with_frontmatter``) are used by ``build_bundled_zip`` to synthesize a
root manifest when the source directory has none, and to keep the manifest
idempotent across re-syncs.
"""

from __future__ import annotations

import io
import os
import re
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from daimon.core.errors import DefaultsError

MAX_FILES: int = 200
MAX_UNCOMPRESSED_BYTES: int = 28 * 1024 * 1024

_SAFE_PATH_RE = re.compile(r"^[A-Za-z0-9._/-]+$")
_ZERO_TS: tuple[int, int, int, int, int, int] = (1980, 1, 1, 0, 0, 0)


@dataclass
class SkillPackage:
    """A built skill zip ready for MA upload."""

    path: Path
    included_paths: list[str]
    dropped_paths: list[str] = field(default_factory=list[str])
    uncompressed_bytes: int = 0


def build_skill_zip(skill_dir: Path, *, name: str | None = None) -> SkillPackage:
    """Build a zip under a temp file; return the :class:`SkillPackage`.

    Every archive entry is prefixed with ``f"{top}/"`` where ``top`` is ``name``
    if provided, otherwise ``skill_dir.name``.
    """
    included: list[tuple[str, Path]] = []
    dropped: list[str] = []
    total_bytes = 0

    root_manifest = skill_dir / "SKILL.md"
    if not root_manifest.is_file():
        raise DefaultsError(f"{skill_dir}: missing SKILL.md")

    top = name if name is not None else skill_dir.name

    for file in sorted(skill_dir.rglob("*")):
        if not file.is_file():
            continue
        rel_posix = file.relative_to(skill_dir).as_posix()

        if not _SAFE_PATH_RE.match(rel_posix):
            dropped.append(rel_posix)
            continue

        if rel_posix != "SKILL.md" and Path(rel_posix).name == "SKILL.md":
            inner = str(Path(rel_posix).parent / "_SKILL.md.txt")
        else:
            inner = rel_posix

        arc_name = f"{top}/{inner}"

        size = file.stat().st_size
        total_bytes += size
        if total_bytes > MAX_UNCOMPRESSED_BYTES:
            raise DefaultsError(
                f"{skill_dir}: uncompressed size exceeds {MAX_UNCOMPRESSED_BYTES} bytes"
            )

        included.append((arc_name, file))
        if len(included) > MAX_FILES:
            raise DefaultsError(f"{skill_dir}: too many files (> {MAX_FILES})")

    fd, tmp_path = tempfile.mkstemp(prefix=f"skill-{top}-", suffix=".zip")
    os.close(fd)
    tmp = Path(tmp_path)
    with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for arc_name, file in included:
            zf.write(file, arcname=arc_name)

    return SkillPackage(
        path=tmp,
        included_paths=[arc for arc, _ in included],
        dropped_paths=dropped,
        uncompressed_bytes=total_bytes,
    )


# ---------------------------------------------------------------------------
# Canonical (deterministic) zip builder
# ---------------------------------------------------------------------------


def canonical_zip_bytes(src_dir: Path, *, arcname_prefix: str) -> bytes:
    """Build a byte-deterministic zip of ``src_dir``.

    File order is lexicographic, timestamps are zeroed (1980-01-01), and
    external attributes are zeroed. Symlinks are skipped (their targets could
    point outside ``src_dir``). Subtrees rooted at a nested ``SKILL.md`` (other
    than ``src_dir``'s own) are excluded — they are their own skill and would
    otherwise produce an archive with multiple ``SKILL.md`` files.
    """
    nested_roots: list[Path] = [p.parent for p in src_dir.rglob("SKILL.md") if p.parent != src_dir]

    def _under_nested(path: Path) -> bool:
        return any(r == path or r in path.parents for r in nested_roots)

    files: list[Path] = sorted(
        p
        for p in src_dir.rglob("*")
        if not p.is_symlink()
        and p.is_file()
        and not _under_nested(p)
        and _SAFE_PATH_RE.match(p.relative_to(src_dir).as_posix())
    )

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for p in files:
            rel = p.relative_to(src_dir).as_posix()
            zi = zipfile.ZipInfo(filename=f"{arcname_prefix}/{rel}")
            zi.date_time = _ZERO_TS
            zi.external_attr = 0
            zi.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(zi, p.read_bytes())
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Skill-name sanitization
# ---------------------------------------------------------------------------

_SKILL_NAME_SUBST = re.compile(r"[^a-z0-9_-]+")
_SKILL_NAME_DASH = re.compile(r"-+")
_SKILL_NAME_MAX = 64


RESERVED_SKILL_NAME_WORDS = ("claude", "anthropic")


def sanitize_skill_name(raw: str) -> str:
    """Sanitize to an Anthropic-safe skill name (character set + length only).

    Rules: lowercase, collapse non-``[a-z0-9_-]`` runs to a single ``-``,
    strip leading/trailing ``-``, truncate to 64 characters. Idempotent:
    ``sanitize(sanitize(x)) == sanitize(x)``.

    Does NOT substitute reserved words ('claude', 'anthropic') — MA validates
    those against the SKILL.md `name:` field inside the uploaded zip, not the
    Python-side name, so substituting here only desyncs the two without
    helping. Detection happens in the bundler (skill_sync.bundler), which
    skips reserved-name skills with a structured warning.
    """
    s = raw.strip().lower()
    s = _SKILL_NAME_SUBST.sub("-", s)
    s = _SKILL_NAME_DASH.sub("-", s).strip("-")
    return s[:_SKILL_NAME_MAX]


# ---------------------------------------------------------------------------
# Frontmatter helpers
# ---------------------------------------------------------------------------

_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?", re.DOTALL)
_FM_FIELD_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_-]*):\s*(.*?)\s*$", re.MULTILINE)


def has_frontmatter(text: str) -> bool:
    """Return ``True`` if ``text`` begins with a ``---``-delimited block."""
    return bool(_FRONTMATTER_RE.match(text))


def parse_skill_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """Return ``(frontmatter_fields, body)``.

    If ``text`` has no leading frontmatter block, the dict is empty and the
    body equals ``text`` verbatim.
    """
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return {}, text
    fields: dict[str, str] = {}
    for mm in _FM_FIELD_RE.finditer(m.group(1)):
        fields[mm.group(1)] = mm.group(2)
    body = text[m.end() :]
    return fields, body


def wrap_with_frontmatter(content: str, fields: dict[str, str]) -> str:
    """Prepend a ``---``-delimited block built from ``fields``.

    Idempotent: returns ``content`` unchanged if it already starts with
    frontmatter.
    """
    if has_frontmatter(content):
        return content
    if not fields:
        return content
    rendered = "\n".join(f"{k}: {v}" for k, v in fields.items())
    return f"---\n{rendered}\n---\n{content}"


# ---------------------------------------------------------------------------
# build_bundled_zip — bundle a whole repo as a single skill
# ---------------------------------------------------------------------------


def build_bundled_zip(src_dir: Path, repo_name: str) -> tuple[bytes, str, list[str]]:
    """Bundle ``src_dir`` as a single skill, deterministically.

    If ``src_dir/SKILL.md`` does not exist, synthesize one with frontmatter
    derived from ``repo_name``. If it does exist but lacks frontmatter, wrap
    it. Other ``SKILL.md`` files nested under ``src_dir`` are excluded
    (handled by :func:`canonical_zip_bytes`).

    Returns ``(zip_bytes, arcname_prefix, included_paths)`` where
    ``arcname_prefix`` is ``sanitize_skill_name(repo_name)`` and
    ``included_paths`` is the lexicographically sorted list of relative paths
    actually placed in the archive (with the ``arcname_prefix`` applied).
    """
    arc = sanitize_skill_name(repo_name) or "skill"
    root_skill = src_dir / "SKILL.md"

    # Synthesize-or-wrap the root manifest in a temp directory so the on-disk
    # source is never mutated. We then run canonical_zip_bytes against the
    # combined view (temp manifest + original src_dir minus the original root).
    with tempfile.TemporaryDirectory(prefix="bundled-skill-") as td:
        staging = Path(td)
        if root_skill.is_file():
            existing = root_skill.read_text(encoding="utf-8", errors="replace")
            if has_frontmatter(existing):
                manifest_text = existing
            else:
                manifest_text = wrap_with_frontmatter(
                    existing,
                    {
                        "name": arc,
                        "description": f"Bundled skill from {repo_name}",
                    },
                )
        else:
            manifest_text = wrap_with_frontmatter(
                "",
                {
                    "name": arc,
                    "description": f"Bundled skill from {repo_name}",
                },
            )

        # Stage every file from src_dir into the temp dir, replacing the root
        # SKILL.md with our (possibly synthesized) manifest. We mirror the
        # exclusion rules of canonical_zip_bytes inline so staging mirrors what
        # actually ends up in the zip.
        nested_roots: list[Path] = [
            p.parent for p in src_dir.rglob("SKILL.md") if p.parent != src_dir
        ]

        def _under_nested(path: Path) -> bool:
            return any(r == path or r in path.parents for r in nested_roots)

        (staging / "SKILL.md").write_text(manifest_text, encoding="utf-8")

        for p in sorted(src_dir.rglob("*")):
            if p.is_symlink() or not p.is_file():
                continue
            if _under_nested(p):
                continue
            rel = p.relative_to(src_dir)
            if rel.as_posix() == "SKILL.md":
                continue  # handled above with manifest_text
            dest = staging / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(p.read_bytes())

        zip_bytes = canonical_zip_bytes(staging, arcname_prefix=arc)

    # Recompute included_paths from the zip itself so the returned list
    # exactly matches archive contents.
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        included = sorted(zf.namelist())
    return zip_bytes, arc, included
