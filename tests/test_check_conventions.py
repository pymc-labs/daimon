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
                "pyproject.toml": '[[tool.importlinter.contracts]]\nname = "ORM"\nforbidden_modules = ["daimon.core._models"]\n'
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


def test_changelog_edit_is_flagged_unless_it_folds_fragments() -> None:
    edit = [("M", "CHANGELOG.md")]
    found = conv.check_diff(edit, history=True)
    assert [v.rule for v in found] == ["changelog-edit"], "a direct edit must fail"
    _assert_explains(found[0], "Changelog")
    fold = [*edit, ("D", "changes/topic.md")]
    assert conv.check_diff(fold, history=True) == [], "a fold deletes fragments and may edit it"
    readme_only = [*edit, ("D", "changes/README.md")]
    assert conv.check_diff(readme_only, history=True), "deleting the README is not a fold"


def test_edited_base_migration_is_flagged_only_against_a_base() -> None:
    path = "packages/core/alembic/versions/0001_a.py"
    found = conv.check_diff([("M", path), ("A", path.replace("0001", "0002"))], history=True)
    assert [(v.rule, v.file) for v in found] == [("migration-edit", path)], "only the edit fails"
    _assert_explains(found[0], "Migrations")
    assert conv.check_diff([("M", path)], history=False) == [], (
        "staged edits may touch own migrations"
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
