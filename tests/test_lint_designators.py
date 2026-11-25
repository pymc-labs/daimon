"""scripts/lint_designators.sh: what the public-repo guard catches and what it prints."""

import os
import shutil
import subprocess
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]


def _repo(tmp_path: Path, files: dict[str, str]) -> Path:
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    shutil.copy(_ROOT / "scripts/lint_designators.sh", repo / "scripts/lint_designators.sh")
    for name, text in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return repo


def _lint(repo: Path, **env: str) -> subprocess.CompletedProcess[str]:
    base = {k: v for k, v in os.environ.items() if not k.startswith("DAIMON_LINT_DENYLIST")}
    base["HOME"] = str(repo / "nohome")
    return subprocess.run(
        ["bash", str(repo / "scripts/lint_designators.sh")],
        capture_output=True,
        text=True,
        check=False,
        env={**base, **env},
    )


def test_denylist_catches_defaults_and_tests_and_prints_only_file_and_line(
    tmp_path: Path,
) -> None:
    repo = _repo(
        tmp_path,
        {
            "defaults/skills/x/SKILL.md": "Sourced from the Zyxwco review.\n",
            "packages/a/tests/test_x.py": 'ROLE = "zyxwco client"\n',
            "docs/page.md": "Nothing to see.\n",
        },
    )
    result = _lint(repo, DAIMON_LINT_DENYLIST="# private\nzyxwco\n")
    assert result.returncode == 1, result.stdout
    assert "defaults/skills/x/SKILL.md:1" in result.stdout
    assert "packages/a/tests/test_x.py:1" in result.stdout
    assert "docs/page.md" not in result.stdout
    assert "zyxwco" not in result.stdout.casefold(), "a public CI log must not echo the name"


def test_denylist_file_and_whole_word_match(tmp_path: Path) -> None:
    repo = _repo(tmp_path, {"tests/test_x.py": "zyxwcorp is a different word\n"})
    names = tmp_path / "names"
    names.write_text("zyxwco\n")
    result = _lint(repo, DAIMON_LINT_DENYLIST_FILE=str(names))
    assert result.returncode == 0, result.stdout
    assert "private denylist checked" in result.stdout


def test_without_a_denylist_the_rule_is_skipped_and_says_so(tmp_path: Path) -> None:
    repo = _repo(tmp_path, {"tests/test_x.py": "Zyxwco\n"})
    result = _lint(repo)
    assert result.returncode == 0, result.stdout
    assert "customer-name rule skipped" in result.stdout


def test_personal_home_directories_fail_and_generic_ones_pass(tmp_path: Path) -> None:
    repo = _repo(
        tmp_path,
        {
            "formal/README.md": "Run with /home/" + "jdoe/tools/tla2tools.jar\n",
            "apps/x/test_x.py": 'PATH = "/home/work/a/b.txt"\n',
        },
    )
    result = _lint(repo)
    assert result.returncode == 1
    assert "FAIL [home-directory]" in result.stdout
    assert "formal/README.md:1" in result.stdout
    assert "apps/x/test_x.py" not in result.stdout


def test_the_repository_itself_is_clean() -> None:
    result = subprocess.run(
        ["bash", str(_ROOT / "scripts/lint_designators.sh")],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout
