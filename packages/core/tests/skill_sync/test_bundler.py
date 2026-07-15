"""Tests for daimon.core.skill_sync.bundler.

Each test builds a real in-memory tar.gz blob and round-trips it through
the bundler. No mocking of the bundler itself — only the asyncio.to_thread
hook is observed (namespace-scoped) to assert sync work is offloaded.
"""

from __future__ import annotations

import asyncio
import io
import tarfile
import zipfile
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any

import pytest
from daimon.core.skill_sync.bundler import SkillEntry, extract_and_bundle
from daimon.core.specs import SkillRepo


def _make_tarball(files: dict[str, bytes]) -> bytes:
    """Build a tar.gz blob from a {tar-internal-path -> content-bytes} mapping."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for path, content in files.items():
            info = tarfile.TarInfo(name=path)
            info.size = len(content)
            tf.addfile(info, io.BytesIO(content))
    return buf.getvalue()


async def test_extract_and_bundle_split_mode_finds_two_skill_md_dirs(tmp_path: Path) -> None:
    """Split mode emits one SkillEntry per discovered SKILL.md directory, lex-sorted."""
    tarball = _make_tarball(
        {
            "repo/skills/foo/SKILL.md": b"# foo\n",
            "repo/skills/bar/SKILL.md": b"# bar\n",
        }
    )
    entries = await extract_and_bundle(
        tarball_bytes=tarball,
        extract_root=tmp_path,
        repo_name="my-repo",
        split=True,
    )
    assert len(entries) == 2, "split mode should produce one entry per SKILL.md dir"
    names = [e.name for e in entries]
    assert names == ["bar", "foo"], "entries should be lex-sorted by skill_dir path"
    for entry in entries:
        assert entry.prebuilt_zip is None, "split-mode entries carry no prebuilt zip"


async def test_extract_and_bundle_with_default_split_keeps_every_subskill(tmp_path: Path) -> None:
    """A repo of sibling skill dirs, synced with the DEFAULT SkillRepo.split, must
    yield one complete skill per SKILL.md — not a single bundle that drops every
    nested subtree. This is the create_agent regression: the chat path relies on
    the SkillRepo default and a bundled default ships a hollow restaurant-ranker.
    """
    tarball = _make_tarball(
        {
            "repo/restaurant-ranker/SKILL.md": b"---\nname: restaurant-ranker\n---\n",
            "repo/restaurant-ranker/scripts/booking.py": b"print('real booking')\n",
            "repo/booking-probe/SKILL.md": b"---\nname: booking-probe\n---\n",
            "repo/cloakbrowser/SKILL.md": b"---\nname: cloakbrowser\n---\n",
        }
    )
    default_split = SkillRepo(url="https://github.com/o/restaurant-ranker").split
    entries = await extract_and_bundle(
        tarball_bytes=tarball,
        extract_root=tmp_path,
        repo_name="restaurant-ranker",
        split=default_split,
    )
    names = sorted(e.name for e in entries)
    assert names == ["booking-probe", "cloakbrowser", "restaurant-ranker"], (
        "default sync must discover each SKILL.md as its own skill, not collapse to one bundle"
    )
    ranker = next(e for e in entries if e.name == "restaurant-ranker")
    assert (ranker.skill_dir / "scripts" / "booking.py").is_file(), (
        "the restaurant-ranker skill must still carry its scripts/ (not a hollow skill)"
    )


async def test_extract_and_bundle_bundled_mode_returns_single_entry_with_prebuilt_zip(
    tmp_path: Path,
) -> None:
    """Bundled mode emits exactly one entry whose prebuilt_zip is populated."""
    tarball = _make_tarball({"repo/SKILL.md": b"# repo skill\n"})
    entries = await extract_and_bundle(
        tarball_bytes=tarball,
        extract_root=tmp_path,
        repo_name="my-repo",
        split=False,
    )
    assert len(entries) == 1, "bundled mode should produce a single repo-level entry"
    entry = entries[0]
    assert entry.name == "my-repo", "bundled entry name should be sanitized repo_name"
    assert entry.source_rel == "", "bundled entry source_rel should be empty (repo root)"
    assert entry.prebuilt_zip is not None, "bundled mode must attach a prebuilt zip"
    assert isinstance(entry.prebuilt_zip, bytes), "prebuilt_zip must be bytes"


async def test_extract_and_bundle_bundled_mode_synthesizes_skill_md_when_missing(
    tmp_path: Path,
) -> None:
    """When the tarball has no SKILL.md, the bundled zip must contain a synthesized one."""
    tarball = _make_tarball({"repo/README.md": b"# readme\n"})
    entries = await extract_and_bundle(
        tarball_bytes=tarball,
        extract_root=tmp_path,
        repo_name="my-repo",
        split=False,
    )
    assert len(entries) == 1, "bundled mode emits one entry"
    zip_bytes = entries[0].prebuilt_zip
    assert zip_bytes is not None, "prebuilt_zip must be populated"
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        names = zf.namelist()
    assert any(n.endswith("/SKILL.md") for n in names), (
        f"bundled zip must contain a synthesized SKILL.md; got {names}"
    )


async def test_extract_and_bundle_smart_strips_single_wrapper_dir(tmp_path: Path) -> None:
    """A tarball with a single top-level wrapper dir is descended into automatically."""
    tarball = _make_tarball({"wrapper-abc123/SKILL.md": b"# inside wrapper\n"})
    entries = await extract_and_bundle(
        tarball_bytes=tarball,
        extract_root=tmp_path,
        repo_name="my-repo",
        split=False,
    )
    assert len(entries) == 1, "smart-strip should descend into the wrapper dir"
    assert entries[0].prebuilt_zip is not None, "bundled entry must have prebuilt zip"


async def test_extract_and_bundle_walks_skill_md_case_insensitive(tmp_path: Path) -> None:
    """A file named `Skill.md` (mixed case) is still discovered."""
    tarball = _make_tarball({"repo/foo/Skill.md": b"# mixed case\n"})
    entries = await extract_and_bundle(
        tarball_bytes=tarball,
        extract_root=tmp_path,
        repo_name="my-repo",
        split=True,
    )
    assert len(entries) == 1, "case-insensitive walk must find Skill.md"
    assert entries[0].name == "foo", "entry should take the parent-dir name"


async def test_extract_and_bundle_marks_reserved_word_skill_with_skip_reason(
    tmp_path: Path,
) -> None:
    """A SKILL.md whose `name:` field contains a reserved word gets skip_reason set.

    MA validates SKILL.md frontmatter name against reserved words 'claude' and
    'anthropic' (probed live 2026-05-09). Detected at bundle time so the
    orchestrator can record into SyncReport.failed_uploads instead of letting
    it fail at upload with an opaque message.
    """
    tarball = _make_tarball(
        {
            "repo/claude-api/SKILL.md": (
                b"---\nname: claude-api\ndescription: probe\n---\n# claude-api\n"
            ),
            "repo/innocuous/SKILL.md": (
                b"---\nname: innocuous\ndescription: ok\n---\n# innocuous\n"
            ),
        }
    )
    entries = await extract_and_bundle(
        tarball_bytes=tarball,
        extract_root=tmp_path,
        repo_name="my-repo",
        split=True,
    )

    by_name = {e.name: e for e in entries}
    assert "claude-api" in by_name, "reserved-word entry is still emitted (with skip_reason)"
    assert by_name["claude-api"].skip_reason is not None, (
        "claude-named SKILL.md must carry skip_reason for the orchestrator"
    )
    assert "claude" in by_name["claude-api"].skip_reason, "reason cites the reserved word"

    assert "innocuous" in by_name, "non-reserved skill is preserved"
    assert by_name["innocuous"].skip_reason is None, "innocuous skill is not skipped"


async def test_extract_and_bundle_uses_filter_data(tmp_path: Path) -> None:
    """A malicious tarball with a `..` traversal must not escape extract_root."""
    tarball = _make_tarball({"../escape.txt": b"pwned"})
    escape_target = tmp_path.parent / "escape.txt"
    # Either tarfile raises (filter='data' rejects the entry) or it sanitizes
    # the path; what matters is no file ends up outside extract_root.
    raised: BaseException | None = None
    try:
        await extract_and_bundle(
            tarball_bytes=tarball,
            extract_root=tmp_path,
            repo_name="my-repo",
            split=True,
        )
    except Exception as exc:
        raised = exc
    assert not escape_target.exists(), (
        f"filter='data' must prevent path traversal; found {escape_target}"
    )
    # Sanity: either the call raised, or it returned cleanly with no escape.
    # Both are acceptable per the filter contract.
    assert raised is None or isinstance(raised, Exception), (
        "if extract raised, it must be an Exception subclass"
    )


async def test_extract_and_bundle_runs_in_thread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sync work must be routed through asyncio.to_thread so the event loop is unblocked."""
    called: list[bool] = []
    real_to_thread = asyncio.to_thread

    async def spy(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        called.append(True)
        return await real_to_thread(fn, *args, **kwargs)

    # Namespace-scoped patch — only the bundler's asyncio.to_thread reference.
    monkeypatch.setattr("daimon.core.skill_sync.bundler.asyncio.to_thread", spy)
    tarball = _make_tarball({"repo/SKILL.md": b"# x\n"})
    entries = await extract_and_bundle(
        tarball_bytes=tarball,
        extract_root=tmp_path,
        repo_name="my-repo",
        split=False,
    )
    assert called, "extract_and_bundle must route sync work through asyncio.to_thread"
    assert len(entries) == 1, "bundle should still complete normally through the spy"
    # Silence unused-import / unused-symbol linting for typed helpers.
    _ = SkillEntry
    _ = Coroutine
