"""scripts/check_conventions.py: each delivery-flow violation and what its message says."""

import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

_ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("conv", _ROOT / "scripts/check_conventions.py")
assert _spec and _spec.loader
conv = importlib.util.module_from_spec(_spec)
sys.modules["conv"] = conv
_spec.loader.exec_module(conv)

_MIGRATION = '"""a\n\n{marker}"""\nrevision = "0001_a"\ndown_revision = None\n\n\ndef upgrade():\n    pass\n\n\ndef downgrade():\n    raise NotImplementedError\n'


def _repo(tmp_path: Path, files: dict[str, str]) -> Path:
    """A git repo holding the real scripts, a clean pyproject and a valid migration, plus `files`."""
    shutil.copytree(
        _ROOT / "scripts", tmp_path / "scripts", ignore=shutil.ignore_patterns("__pycache__")
    )
    base = {
        "pyproject.toml": "[tool.importlinter]\n",
        "packages/core/alembic/versions/0001_a.py": _MIGRATION.format(
            marker="downgrade: unsupported\n"
        ),
        "packages/core/alembic/head.txt": "0001_a\n",
        "changes/README.md": "readme\n",
    }
    for name, text in {**base, **files}.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    subprocess.run(["git", "-C", str(tmp_path), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "add", "-A"], check=True)
    return tmp_path


def _assert_explains(violation: object, section: str) -> None:
    text = violation.message()  # type: ignore[attr-defined]
    for part in ("Why: ", "Fix: ", "Read: ", section, "on latest main"):
        assert part in text, f"message lacks {part!r}: {text}"


def test_clean_repo_has_no_violations(tmp_path: Path) -> None:
    root = _repo(tmp_path, {})
    assert conv.check_tree(root) == [], "tree checks flag a clean repo"
    assert conv.check_scripts(root) == [], "script checks flag a clean repo"


@pytest.mark.parametrize(
    ("files", "rule", "section"),
    [
        ({"docs/features.md": "x\n"}, "generated-page", "Documentation"),
        ({"docs/mcp-tools.md": "x\n"}, "generated-page", "Documentation"),
        (
            {"packages/adapters/mcp/tests/__snapshots__/a.ambr": "x\n"},
            "snapshot-ambr",
            "Quality gates",
        ),
        (
            {
                "pyproject.toml": '[[tool.importlinter.contracts]]\nname = "ORM"\n'
                f"forbidden_modules = [{'.'.join(('daimon', 'core', '_models'))!r}]\n"
            },
            "orm-contract",
            "Repo tour",
        ),
    ],
)
def test_tree_violations(tmp_path: Path, files: dict[str, str], rule: str, section: str) -> None:
    found = conv.check_tree(_repo(tmp_path, files))
    assert [v.rule for v in found] == [rule], f"expected only {rule}, got {found}"
    _assert_explains(found[0], section)


def _git(repo: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return done.stdout


def _history(tmp_path: Path) -> Path:
    """A repo with CHANGELOG.md, two fragments and a migration, committed as the base."""
    files = {
        "CHANGELOG.md": "# Changelog\n\n## [Unreleased]\n\n- **Old.** entry.\n",
        "changes/a.md": "- **A.**\n  Body a.\n",
        "changes/b.md": "- **B.**\n  Body b.\n",
        "packages/core/alembic/versions/0001_a.py": "x = 1\n",
        "packages/core/alembic/versions/helper.py": "x = 1\n",
    }
    for name, text in files.items():
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text(text)
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "base")
    return tmp_path


def _violations(repo: Path, *, staged: bool = False) -> list[object]:
    args = conv.diff_args(repo, None if staged else "HEAD~1", staged)
    return conv.check_diff(repo, args)


def _commit(repo: Path) -> None:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "change")


def test_changelog_edit_is_flagged(tmp_path: Path) -> None:
    repo = _history(tmp_path)
    (repo / "CHANGELOG.md").write_text("# Changelog\n\n## [Unreleased]\n\n- **Mine.** by hand.\n")
    _commit(repo)
    found = _violations(repo)
    assert [v.rule for v in found] == ["changelog-edit"], "a direct edit must fail"  # type: ignore[attr-defined]
    _assert_explains(found[0], "Changelog")


def test_a_fold_may_edit_the_changelog_but_a_stray_line_may_not(tmp_path: Path) -> None:
    repo = _history(tmp_path)
    folded = "# Changelog\n\n## [Unreleased]\n\n- **A.**\n  Body a.\n\n- **Old.** entry.\n"
    (repo / "CHANGELOG.md").write_text(folded)
    (repo / "changes/a.md").unlink()
    _commit(repo)
    assert _violations(repo) == [], "folding a.md adds exactly its lines"
    _git(repo, "reset", "-q", "--hard", "HEAD~1")
    # Delete an unrelated fragment and slip in a hand-written line.
    (repo / "CHANGELOG.md").write_text(folded.replace("Body a.", "Body a.\n  Hand-written."))
    (repo / "changes/b.md").unlink()
    _commit(repo)
    found = _violations(repo)
    assert [v.rule for v in found] == ["changelog-edit"], (
        "lines no deleted fragment holds must fail"
    )  # type: ignore[attr-defined]
    assert "- **A.**" in found[0].message(), "the message names the stray line"  # type: ignore[attr-defined]


def test_only_numbered_base_migrations_are_protected(tmp_path: Path) -> None:
    repo = _history(tmp_path)
    hexed = "14d362cce079_file_uploads.py"
    (repo / "packages/core/alembic/versions" / hexed).write_text("x = 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "hex")
    for name in ("0001_a.py", hexed, "helper.py"):
        (repo / "packages/core/alembic/versions" / name).write_text("x = 2\n")
    _commit(repo)
    found = _violations(repo)
    assert [v.file for v in found] == [
        "packages/core/alembic/versions/0001_a.py",
        f"packages/core/alembic/versions/{hexed}",
    ], f"got {found}"
    _assert_explains(found[0], "Migrations")


def test_staged_edit_of_a_migration_is_not_flagged(tmp_path: Path) -> None:
    repo = _history(tmp_path)
    (repo / "packages/core/alembic/versions/0001_a.py").write_text("x = 2\n")
    _git(repo, "add", "-A")
    assert _violations(repo, staged=True) == [], (
        "staged edits may touch the branch's own migrations"
    )


def test_staged_diff_is_skipped_while_merging(tmp_path: Path) -> None:
    repo = _history(tmp_path)
    (repo / "CHANGELOG.md").write_text("# Changelog\n\n- **Main's fold.**\n")
    _git(repo, "add", "-A")
    assert _violations(repo, staged=True), "a staged changelog edit fails outside a merge"
    (repo / ".git/MERGE_HEAD").write_text(_git(repo, "rev-parse", "HEAD"))
    assert _violations(repo, staged=True) == [], (
        "main's own fold in a merge is not the author's edit"
    )


def test_malformed_fragment_names_the_file_and_the_fix(tmp_path: Path) -> None:
    found = conv.check_scripts(_repo(tmp_path, {"changes/topic.md": "no lead\n"}))
    assert [v.rule for v in found] == ["changelog-fragment"], f"got {found}"
    assert found[0].file == "changes/topic.md", "the annotation must point at the fragment"
    _assert_explains(found[0], "Changelog")


def test_stale_head_says_to_merge_main_and_rechain(tmp_path: Path) -> None:
    found = conv.check_scripts(_repo(tmp_path, {"packages/core/alembic/head.txt": "0000_old\n"}))
    assert [v.rule for v in found] == ["migration-chain"], f"got {found}"
    assert "merge main, run scripts/rechain_migrations.py" in found[0].message(), "no fix command"
    _assert_explains(found[0], "Migrations")


def test_missing_marker_is_reported_with_its_file(tmp_path: Path) -> None:
    path = "packages/core/alembic/versions/0001_a.py"
    found = conv.check_scripts(_repo(tmp_path, {path: _MIGRATION.format(marker="")}))
    assert [v.rule for v in found] == ["migration-markers"], f"got {found}"
    assert found[0].file == path, "the annotation must point at the migration"
    _assert_explains(found[0], "Migrations")


def test_report_emits_one_escaped_error_annotation(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    conv.report([conv.Violation("changelog-edit", "CHANGELOG.md")])
    out = capsys.readouterr().out.strip()
    assert out.startswith("::error file=CHANGELOG.md,title=conventions%3A changelog-edit::"), out
    assert "\n" not in out and "%0AWhy: " in out, "newlines must be annotation-escaped"


def test_ci_runs_conventions_on_every_pull_request() -> None:
    workflow = yaml.safe_load((_ROOT / ".github/workflows/ci.yml").read_text())
    trigger = workflow.get("on", workflow.get(True))
    assert trigger["pull_request"] in (None, {}), (
        "a path filter would stop the required check reporting"
    )
    job = workflow["jobs"]["conventions"]
    assert "if" not in job and "needs" not in job, "the job must always run on its own"
