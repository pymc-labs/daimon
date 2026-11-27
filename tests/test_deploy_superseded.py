"""A main deploy that main has already moved past skips cleanly; the head always deploys.

`scripts/ci/deploy_superseded.sh` against real git repositories (a bare origin and
a shallow, one-commit checkout like actions/checkout's), and the deploy workflow's
contract: the check runs right after checkout, once the job holds its concurrency
slot, and every later step is skipped on a superseded run.
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "ci" / "deploy_superseded.sh"
DEPLOY = ROOT / ".github" / "workflows" / "deploy.yml"
GUARD = "steps.fresh.outputs.superseded != 'true'"


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "init.defaultBranch=main", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        env={
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@example.test",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.test",
            "PATH": "/usr/bin:/bin",
            "HOME": str(cwd),
        },
    ).stdout.strip()


@pytest.fixture
def origin(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    """A bare origin whose main is A <- B <- C, plus a commit D on a side branch."""
    work = tmp_path / "work"
    work.mkdir()
    git(work, "init", "-q")
    commits: dict[str, str] = {}
    for name in ("A", "B", "C"):
        (work / "f").write_text(name)
        git(work, "add", "f")
        git(work, "commit", "-qm", name)
        commits[name] = git(work, "rev-parse", "HEAD")
    git(work, "checkout", "-qb", "side", commits["A"])
    (work / "g").write_text("D")
    git(work, "add", "g")
    git(work, "commit", "-qm", "D")
    commits["D"] = git(work, "rev-parse", "HEAD")
    bare = tmp_path / "origin.git"
    git(tmp_path, "clone", "-q", "--bare", str(work), str(bare))
    return bare, commits


def checkout(tmp_path: Path, bare: Path, sha: str) -> Path:
    """Like actions/checkout: one commit, shallow, at `sha`."""
    repo = tmp_path / f"checkout-{sha[:7]}"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "remote", "add", "origin", f"file://{bare}")
    git(repo, "fetch", "-q", "--depth=1", "origin", sha)
    git(repo, "checkout", "-q", "FETCH_HEAD")
    return repo


def run_script(
    repo: Path, sha: str, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(SCRIPT), sha, "origin"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, "GITHUB_REPOSITORY": "", **(env or {})},
    )


def decide(repo: Path, sha: str, env: dict[str, str] | None = None) -> dict[str, str]:
    out = run_script(repo, sha, env).stdout
    return dict(line.split("=", 1) for line in out.splitlines() if "=" in line)


def fake_gh(tmp_path: Path, head: str, compare: str) -> dict[str, str]:
    """An env whose `gh api` answers commits/main with `head` and compare with `compare` (or fails)."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    gh = bin_dir / "gh"
    gh.write_text(
        "#!/bin/sh\n"
        'case "$2" in\n'
        f"  */commits/main) echo {head} ;;\n"
        f"  */compare/*) {compare} ;;\n"
        "  *) exit 1 ;;\n"
        "esac\n"
    )
    gh.chmod(gh.stat().st_mode | stat.S_IEXEC)
    return {"PATH": f"{bin_dir}:{os.environ['PATH']}", "GITHUB_REPOSITORY": "o/r"}


@pytest.mark.parametrize(
    ("commit", "superseded", "reason"),
    [
        ("C", "false", "head"),  # the newest run always deploys
        ("B", "true", "ancestor"),  # an older main run: the newer one deploys a superset
        ("A", "true", "ancestor"),
        ("D", "false", "not_ancestor"),  # off main: never skipped on a guess
    ],
)
def test_only_an_older_main_commit_is_superseded(
    tmp_path: Path, origin: tuple[Path, dict[str, str]], commit: str, superseded: str, reason: str
) -> None:
    bare, commits = origin
    repo = checkout(tmp_path, bare, commits[commit])
    assert git(repo, "rev-parse", "--is-shallow-repository") == "true"
    result = decide(repo, commits[commit])
    assert (result["superseded"], result["reason"]) == (superseded, reason)
    assert result["head"] == commits["C"]


def test_an_unreadable_main_deploys(tmp_path: Path, origin: tuple[Path, dict[str, str]]) -> None:
    bare, commits = origin
    repo = checkout(tmp_path, bare, commits["B"])
    git(repo, "remote", "set-url", "origin", f"file://{tmp_path / 'gone.git'}")
    done = run_script(repo, commits["B"])
    assert "reason=fetch_failed" in done.stdout and "superseded=false" in done.stdout
    assert "retrying without --filter" in done.stderr, "the second attempt must be logged"
    assert "gone.git" in done.stderr, "git's own error text must reach the log"


@pytest.mark.parametrize(
    ("commit", "compare", "superseded", "reason"),
    [
        ("B", "printf 'ahead\\t0\\n'", "true", "api_ancestor"),
        ("B", "printf 'diverged\\t2\\n'", "false", "api_not_ancestor"),
        ("B", "exit 1", "false", "fetch_failed"),
        ("C", "exit 1", "false", "head"),
    ],
)
def test_the_api_answers_when_git_cannot(
    tmp_path: Path,
    origin: tuple[Path, dict[str, str]],
    commit: str,
    compare: str,
    superseded: str,
    reason: str,
) -> None:
    bare, commits = origin
    repo = checkout(tmp_path, bare, commits[commit])
    git(repo, "remote", "set-url", "origin", f"file://{tmp_path / 'gone.git'}")
    result = decide(repo, commits[commit], fake_gh(tmp_path, commits["C"], compare))
    assert (result["superseded"], result["reason"]) == (superseded, reason)


def _steps() -> list[dict[str, Any]]:
    workflow = yaml.safe_load(DEPLOY.read_text())
    return workflow["jobs"]["deploy"]["steps"]


def test_the_check_runs_first_and_only_once() -> None:
    steps = _steps()
    ids = [step.get("id") for step in steps]
    first = ids.index("fresh")
    assert steps[first - 1].get("uses", "").startswith("actions/checkout@"), (
        "the freshness check is the first step after checkout, before any gcloud"
    )
    # Once: after migrations and the MCP deploy, skipping would leave a half-deployed
    # staging. A queued older run starts only after the newer one finishes.
    assert sum("deploy_superseded.sh" in step.get("run", "") for step in steps) == 1
    assert steps[first]["env"]["GH_TOKEN"] == "${{ github.token }}", (
        "the API fallback needs the token"
    )
    condition = steps[first]["if"]
    # Only a main CI push: promote.yml and a manual dispatch deploy what they name.
    assert "github.event_name == 'push'" in condition
    assert "github.ref == 'refs/heads/main'" in condition


def test_every_step_after_the_check_is_skipped_on_a_superseded_run() -> None:
    steps = _steps()
    ids = [step.get("id") for step in steps]
    for step in steps[ids.index("fresh") + 1 :]:
        assert GUARD in step.get("if", ""), (
            f"{step.get('name') or step.get('uses')}: a superseded deploy must skip it"
        )
