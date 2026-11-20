"""scripts/rechain_migrations.py renumbers a branch's migrations onto main and checks the chain."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

_SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "rechain_migrations.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("rechain_migrations", _SCRIPT)
    assert spec is not None and spec.loader is not None, "failed to build module spec"
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


rechain_migrations = _load()


def _git(root: Path, *args: str, check: bool = True) -> None:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        cwd=root,
        check=check,
        capture_output=True,
    )


def _migration(root: Path, stem: str, parent: str | None) -> None:
    versions = root / rechain_migrations.VERSIONS
    versions.mkdir(parents=True, exist_ok=True)
    (versions / f"{stem}.py").write_text(
        f'revision: str = "{stem}"\ndown_revision: str | None = {parent!r}\n'
    )


def _commit(root: Path, message: str) -> None:
    _git(root, "add", "-A")
    _git(root, "commit", "-m", message)


@pytest.fixture
def forked_repo(tmp_path: Path) -> Path:
    """`main` gained 0003_other while `feature` added two migrations and a test naming them."""
    _git(tmp_path, "init", "-b", "main")
    _migration(tmp_path, "0002_base", None)
    (tmp_path / rechain_migrations.HEAD_FILE).write_text("0002_base\n")
    _commit(tmp_path, "base")
    _git(tmp_path, "checkout", "-b", "feature")
    _migration(tmp_path, "0003_first", "0002_base")
    _migration(tmp_path, "0004_second", "0003_first")
    (tmp_path / rechain_migrations.HEAD_FILE).write_text("0004_second\n")
    (tmp_path / "test_first.py").write_text('MIGRATION = "0003_first"\nOTHER = "0003_first_x"\n')
    _commit(tmp_path, "feature")
    _git(tmp_path, "checkout", "main")
    _migration(tmp_path, "0003_other", "0002_base")
    (tmp_path / rechain_migrations.HEAD_FILE).write_text("0003_other\n")
    _commit(tmp_path, "main moves")
    _git(tmp_path, "checkout", "feature")
    _git(tmp_path, "merge", "main", "--no-edit", check=False)  # conflicts on head.txt only
    _git(tmp_path, "checkout", "--theirs", str(rechain_migrations.HEAD_FILE))
    _commit(tmp_path, "merge main")
    return tmp_path


def test_rechain_renumbers_the_branch_migrations_after_main_and_is_idempotent(
    forked_repo: Path,
) -> None:
    mapping = rechain_migrations.rechain(forked_repo, "main")
    versions = forked_repo / rechain_migrations.VERSIONS
    assert mapping == {
        "0003_first": "0004_first",
        "0004_second": "0005_second",
    }, "branch migrations follow main's 0003"
    assert sorted(p.stem for p in versions.glob("*.py")) == [
        "0002_base",
        "0003_other",
        "0004_first",
        "0005_second",
    ], "main's file is untouched and the branch files are renamed"
    assert 'down_revision: str | None = "0003_other"' in (
        versions / "0004_first.py"
    ).read_text().replace("'", '"'), "the first branch migration revises main's head"
    assert (versions / "0005_second.py").read_text().count("0004_first") == 1
    assert (forked_repo / "test_first.py").read_text() == (
        'MIGRATION = "0004_first"\nOTHER = "0003_first_x"\n'
    ), "references are rewritten as whole ids only"
    assert (forked_repo / rechain_migrations.HEAD_FILE).read_text() == "0005_second\n"
    assert rechain_migrations.check(forked_repo) == [], "the rechained tree is a valid chain"
    assert rechain_migrations.rechain(forked_repo, "main") == {}, "a second run changes nothing"


def test_check_reports_a_second_head_and_a_stale_head_file(forked_repo: Path) -> None:
    errors = rechain_migrations.check(forked_repo)
    assert any("expected one head" in e for e in errors), "two unchained branches have two heads"
    versions = forked_repo / rechain_migrations.VERSIONS
    (versions / "0004_second.py").unlink()
    (versions / "0003_first.py").unlink()
    (forked_repo / rechain_migrations.HEAD_FILE).write_text("0002_base\n")
    errors = rechain_migrations.check(forked_repo)
    assert any("head.txt" in e and "0003_other" in e for e in errors), (
        "head.txt must name the single head"
    )


def test_rechain_refuses_a_rename_onto_an_existing_file_before_writing(forked_repo: Path) -> None:
    """Two branch migrations with one slug: 0003_x would become 0004_x while 0004_x exists."""
    versions = forked_repo / rechain_migrations.VERSIONS
    (versions / "0004_second.py").unlink()
    _migration(forked_repo, "0004_first", "0003_first")
    before = {p.name: p.read_text() for p in forked_repo.rglob("*.py") if ".git" not in p.parts}

    with pytest.raises(SystemExit, match="0004_first.py: that file exists"):
        rechain_migrations.rechain(forked_repo, "main")

    after = {p.name: p.read_text() for p in forked_repo.rglob("*.py") if ".git" not in p.parts}
    assert after == before, "nothing is rewritten when a rename would collide"
