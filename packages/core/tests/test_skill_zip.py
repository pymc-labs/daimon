from __future__ import annotations

import hashlib
import zipfile
from pathlib import Path

import pytest
from daimon.core.errors import DefaultsError
from daimon.core.skill_zip import (
    MAX_FILES,
    MAX_UNCOMPRESSED_BYTES,
    build_bundled_zip,
    build_skill_zip,
    canonical_zip_bytes,
    has_frontmatter,
    parse_skill_frontmatter,
    sanitize_skill_name,
    wrap_with_frontmatter,
)


def _write(p: Path, body: str = "x") -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body)


# ---------------------------------------------------------------------------
# Existing build_skill_zip behavior — ported from defaults/test_skill_zip.py
# ---------------------------------------------------------------------------


def test_build_skill_zip_basic(tmp_path: Path) -> None:
    d = tmp_path / "brainstorming"
    _write(d / "SKILL.md", "---\nname: brainstorming\ndescription: d\n---\nbody\n")
    _write(d / "references" / "a.md", "ref\n")

    pkg = build_skill_zip(d)
    with zipfile.ZipFile(pkg.path) as zf:
        names = set(zf.namelist())
    assert "brainstorming/SKILL.md" in names, (
        "root manifest should be present under top-level folder"
    )
    assert "brainstorming/references/a.md" in names, (
        "nested file must be archived under top-level folder"
    )


def test_build_skill_zip_nested_skill_md_renamed(tmp_path: Path) -> None:
    d = tmp_path / "s"
    _write(d / "SKILL.md", "---\nname: s\ndescription: d\n---\n")
    _write(d / "examples" / "SKILL.md", "not the root manifest\n")
    pkg = build_skill_zip(d)
    with zipfile.ZipFile(pkg.path) as zf:
        names = set(zf.namelist())
    assert "s/examples/_SKILL.md.txt" in names, "nested SKILL.md must be renamed to _SKILL.md.txt"
    assert "s/examples/SKILL.md" not in names, (
        "nested SKILL.md must not appear under its original name"
    )


def test_build_skill_zip_drops_unsafe_paths(tmp_path: Path) -> None:
    d = tmp_path / "s"
    _write(d / "SKILL.md", "---\nname: s\ndescription: d\n---\n")
    _write(d / "with space.txt", "x")
    pkg = build_skill_zip(d)
    assert "with space.txt" in pkg.dropped_paths, "unsafe path must be dropped"


def test_build_skill_zip_caps_file_count(tmp_path: Path) -> None:
    d = tmp_path / "s"
    _write(d / "SKILL.md", "---\nname: s\ndescription: d\n---\n")
    for i in range(MAX_FILES + 5):
        _write(d / f"f{i}.txt", "x")
    with pytest.raises(DefaultsError, match="too many files"):
        build_skill_zip(d)


def test_build_skill_zip_caps_size(tmp_path: Path) -> None:
    d = tmp_path / "s"
    _write(d / "SKILL.md", "---\nname: s\ndescription: d\n---\n")
    _write(d / "big.bin", "x" * (MAX_UNCOMPRESSED_BYTES + 1))
    with pytest.raises(DefaultsError, match="exceeds"):
        build_skill_zip(d)


def test_build_skill_zip_places_every_entry_under_top_level_folder_when_built(
    tmp_path: Path,
) -> None:
    d = tmp_path / "brainstorming"
    _write(d / "SKILL.md", "---\nname: brainstorming\ndescription: d\n---\n")
    _write(d / "references" / "a.md", "ref\n")
    _write(d / "examples" / "SKILL.md", "nested manifest\n")

    pkg = build_skill_zip(d)

    with zipfile.ZipFile(pkg.path) as zf:
        names = zf.namelist()
    assert all(n.startswith("brainstorming/") for n in names), (
        f"every archive entry must be under the top-level folder; got {names!r}"
    )


# ---------------------------------------------------------------------------
# canonical_zip_bytes
# ---------------------------------------------------------------------------


def test_canonical_zip_bytes_is_deterministic(tmp_path: Path) -> None:
    _write(tmp_path / "SKILL.md", "---\nname: x\ndescription: d\n---\n")
    _write(tmp_path / "a.md", "alpha\n")
    _write(tmp_path / "sub" / "b.md", "beta\n")

    b1 = canonical_zip_bytes(tmp_path, arcname_prefix="x")
    b2 = canonical_zip_bytes(tmp_path, arcname_prefix="x")
    assert hashlib.sha256(b1).hexdigest() == hashlib.sha256(b2).hexdigest(), (
        "canonical_zip_bytes must be byte-stable across calls"
    )


def test_canonical_zip_bytes_excludes_symlinks(tmp_path: Path) -> None:
    _write(tmp_path / "real.txt", "real\n")
    (tmp_path / "link.txt").symlink_to(tmp_path / "real.txt")

    data = canonical_zip_bytes(tmp_path, arcname_prefix="x")
    with zipfile.ZipFile(__import__("io").BytesIO(data)) as zf:
        names = set(zf.namelist())
    assert "x/real.txt" in names, "real file must be present"
    assert "x/link.txt" not in names, "symlink must be excluded for safety"


def test_canonical_zip_bytes_excludes_paths_with_invalid_characters(tmp_path: Path) -> None:
    """Files whose relative POSIX path contains characters outside ``[A-Za-z0-9._/-]``
    must be excluded from the archive — Managed Agents' zip validator rejects them
    with ``400: Zip file contains path with invalid characters``.

    This mirrors the safe-path filter already applied in ``build_skill_zip`` (the
    chat path); the panel path's ``canonical_zip_bytes`` was missing it, which
    caused panel-path syncs of public repos with non-ASCII or special-char file
    names to fail at MA upload while the same repo synced cleanly via chat.
    """
    _write(tmp_path / "SKILL.md", "---\nname: root\n---\n")
    _write(tmp_path / "ok.md", "kept\n")
    _write(tmp_path / "with space.md", "spaces drop\n")
    _write(tmp_path / "中文.md", "unicode drops\n")

    data = canonical_zip_bytes(tmp_path, arcname_prefix="x")
    with zipfile.ZipFile(__import__("io").BytesIO(data)) as zf:
        names = set(zf.namelist())
    assert "x/SKILL.md" in names, "SKILL.md must be kept"
    assert "x/ok.md" in names, "safe ASCII files must be kept"
    assert not any(" " in n for n in names), "files with spaces in path must be dropped"
    assert not any("中" in n for n in names), "files with non-ASCII characters must be dropped"


def test_canonical_zip_bytes_excludes_files_under_nested_skill_md(tmp_path: Path) -> None:
    _write(tmp_path / "SKILL.md", "---\nname: root\n---\n")
    _write(tmp_path / "top.md", "top\n")
    _write(tmp_path / "sub" / "SKILL.md", "nested\n")
    _write(tmp_path / "sub" / "file.txt", "should be excluded\n")

    data = canonical_zip_bytes(tmp_path, arcname_prefix="x")
    with zipfile.ZipFile(__import__("io").BytesIO(data)) as zf:
        names = set(zf.namelist())
    assert "x/top.md" in names, "top-level files outside nested SKILL.md should be present"
    assert "x/sub/file.txt" not in names, "files under nested SKILL.md root must be excluded"
    assert "x/sub/SKILL.md" not in names, "nested SKILL.md itself must be excluded"


# ---------------------------------------------------------------------------
# sanitize_skill_name
# ---------------------------------------------------------------------------


def test_sanitize_skill_name_collapses_unsafe_chars() -> None:
    assert sanitize_skill_name("My Skill!") == "my-skill", (
        "spaces and punctuation collapse to single dash"
    )


def test_sanitize_skill_name_preserves_underscores() -> None:
    assert sanitize_skill_name("foo_bar") == "foo_bar", "underscores must be preserved"


def test_sanitize_skill_name_is_idempotent() -> None:
    raw = "Some Funky Name!!"
    once = sanitize_skill_name(raw)
    twice = sanitize_skill_name(once)
    assert once == twice, "sanitize_skill_name must be idempotent"


# ---------------------------------------------------------------------------
# Frontmatter helpers
# ---------------------------------------------------------------------------


def test_parse_skill_frontmatter_extracts_yaml_block() -> None:
    text = "---\nname: x\ndescription: d\n---\nbody text\n"
    fm, body = parse_skill_frontmatter(text)
    assert fm == {"name": "x", "description": "d"}, "frontmatter dict must contain name+description"
    assert body == "body text\n", "body must be the post-frontmatter remainder"


def test_parse_skill_frontmatter_returns_empty_when_absent() -> None:
    fm, body = parse_skill_frontmatter("plain text\n")
    assert fm == {}, "absent frontmatter must yield empty dict"
    assert body == "plain text\n", "body must equal the input when no frontmatter"


def test_has_frontmatter_detects_leading_dashes() -> None:
    assert has_frontmatter("---\nname: x\n---\nbody"), "must detect leading dashes"
    assert not has_frontmatter("plain text"), (
        "plain text must not be detected as having frontmatter"
    )


def test_wrap_with_frontmatter_is_noop_when_present() -> None:
    text = "---\nname: x\n---\nbody"
    out = wrap_with_frontmatter(text, {"name": "y"})
    assert out == text, "wrap_with_frontmatter must be a no-op when frontmatter already present"


def test_wrap_with_frontmatter_prepends_when_absent() -> None:
    out = wrap_with_frontmatter("body", {"name": "x", "description": "d"})
    fm, body = parse_skill_frontmatter(out)
    assert fm.get("name") == "x", "wrapped frontmatter must contain name"
    assert fm.get("description") == "d", "wrapped frontmatter must contain description"
    assert body == "body", "body must be preserved after wrapping"


# ---------------------------------------------------------------------------
# build_bundled_zip
# ---------------------------------------------------------------------------


def test_build_bundled_zip_synthesizes_skill_md_when_missing(tmp_path: Path) -> None:
    _write(tmp_path / "a.md", "content\n")
    _write(tmp_path / "sub" / "b.md", "more\n")

    zip_bytes, prefix, included = build_bundled_zip(tmp_path, "my-repo")

    assert prefix == "my-repo", "arcname_prefix must be sanitized repo_name"
    with zipfile.ZipFile(__import__("io").BytesIO(zip_bytes)) as zf:
        names = set(zf.namelist())
        skill_md = zf.read(f"{prefix}/SKILL.md").decode("utf-8")
    assert f"{prefix}/SKILL.md" in names, "synthesized SKILL.md must be at archive root"
    assert has_frontmatter(skill_md), "synthesized SKILL.md must have frontmatter"
    assert any(n.endswith("/a.md") for n in names), "other files must be included"
    assert isinstance(included, list), "included_paths must be a list"
    assert all(isinstance(p, str) for p in included), "included_paths must be strings"


def test_build_bundled_zip_is_deterministic(tmp_path: Path) -> None:
    _write(tmp_path / "a.md", "content\n")
    _write(tmp_path / "sub" / "b.md", "more\n")

    b1, _, _ = build_bundled_zip(tmp_path, "my-repo")
    b2, _, _ = build_bundled_zip(tmp_path, "my-repo")
    assert hashlib.sha256(b1).hexdigest() == hashlib.sha256(b2).hexdigest(), (
        "build_bundled_zip must be byte-stable across calls"
    )


def test_build_bundled_zip_preserves_existing_skill_md(tmp_path: Path) -> None:
    _write(tmp_path / "SKILL.md", "---\nname: original\ndescription: orig\n---\nbody\n")
    _write(tmp_path / "a.md", "content\n")

    zip_bytes, prefix, _ = build_bundled_zip(tmp_path, "my-repo")
    with zipfile.ZipFile(__import__("io").BytesIO(zip_bytes)) as zf:
        skill_md = zf.read(f"{prefix}/SKILL.md").decode("utf-8")
    fm, _ = parse_skill_frontmatter(skill_md)
    assert fm.get("name") == "original", "existing SKILL.md frontmatter must be preserved"
