"""scripts/ci/plan.py: which CI jobs a change runs and what a staging deploy refreshes."""

import importlib.util
import json
import os
import re
import shlex
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

_ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("ci_plan", _ROOT / "scripts/ci/plan.py")
assert _spec and _spec.loader
plan = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(plan)


def _ci() -> dict[str, Any]:
    return yaml.safe_load((_ROOT / ".github/workflows/ci.yml").read_text())["jobs"]


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("README.md", "docs"),
        ("docs/quickstart.md", "docs"),
        ("docs/mcp-tools.md", "mcp-catalogue"),
        ("defaults/skills/x/SKILL.md", "core"),
        ("packages/adapters/web/daimon/adapters/web/app.py", "web"),
        ("packages/adapters/web/README.md", "web"),
        ("web/src/App.tsx", "spa"),
        ("tests/parity/test_x.py", "parity"),
        ("tests/golden/turn.json", "golden"),
        ("packages/testing/daimon/testing/asgi.py", "testing"),
        ("packages/core/daimon/core/x.py", "core"),
        ("apps/report-host/src/report_host/app.py", "report-host"),
        ("pyproject.toml", "all"),
        ("uv.lock", "all"),
        (".github/workflows/ci.yml", "all"),
        ("scripts/ci/plan.py", "all"),
        ("newfile", "all"),
    ],
)
def test_area(path: str, expected: str) -> None:
    assert plan.area(path) == expected


def _tests(paths: list[str] | None) -> tuple[set[str], set[str]]:
    result = plan.plan_tests(paths)
    return set(json.loads(result["suites"])), set(json.loads(result["jobs"]))


def test_unknown_base_or_root_file_runs_everything() -> None:
    every = (set(plan.ALL_SUITES), set(plan.JOBS))
    assert _tests(None) == every
    assert _tests(["docs/x.md", "pyproject.toml"]) == every


def test_prose_runs_nothing() -> None:
    assert _tests(["README.md", "docs/self-hosting.md", "CHANGELOG.md"]) == (set(), set())


def test_web_adapter_change_runs_only_its_suites() -> None:
    suites, jobs = _tests(["packages/adapters/web/daimon/adapters/web/app.py"])
    assert suites == {"web", "parity", "parity-mux", "lint", "shard-check", "image-daimon"}
    assert jobs == {"lint", "pytest-db", "pytest-mux-leg", "shard-check", "docker-build"}


def test_test_helpers_run_every_pytest_job() -> None:
    _, jobs = _tests(["packages/testing/daimon/testing/asgi.py"])
    assert jobs == set(plan.JOBS) - {"web", "docker-build", "harness-image"}


def test_a_journey_file_runs_the_journeys() -> None:
    suites, _ = _tests(["tests/parity/test_j3_handover_e2e.py"])
    assert "journeys" in suites


def test_jobs_match_ci_yml() -> None:
    jobs = _ci()
    for name, owned in plan.JOBS.items():
        job = jobs[name]
        assert job["needs"] == "changes"
        assert f"contains(fromJSON(needs.changes.outputs.jobs), '{name}')" in job["if"]
        legs = job.get("strategy", {}).get("matrix", {}).get("include")
        if legs and "suite" in legs[0]:
            assert {leg["suite"] for leg in legs} == set(owned), name
    selected = {"changes", "journeys-offline", "docker-publish-candidate", "deploy-gcp"}
    assert set(jobs) == set(plan.JOBS) | selected


def _gated(job: dict[str, Any], suite: str) -> None:
    for step in job["steps"]:
        assert f"contains(fromJSON(needs.changes.outputs.suites), {suite})" in step["if"], step


def test_matrix_legs_skip_unselected_suites() -> None:
    jobs = _ci()
    for name in ("pytest-db", "pytest-mux-leg", "pytest-light"):
        _gated(jobs[name], "matrix.suite")
    # A required check: it always runs and skips its steps instead.
    assert "if" not in jobs["journeys-offline"]
    _gated(jobs["journeys-offline"], "'journeys'")


# Test files must not import a leaf area's package unless the planner selects their
# suite for that area, or a change there would skip the tests that cover it.
_LEAVES = {
    "web": r"daimon\.adapters\.web\b",
    "harness": r"\bdaimon_harness\b",
    "notebook-host": r"\bnotebook_host\b",
    "report-host": r"\breport_host\b",
}


def _suite_files() -> dict[str, set[Path]]:
    files: dict[str, set[Path]] = {}
    for job in _ci().values():
        for leg in job.get("strategy", {}).get("matrix", {}).get("include", []):
            if "suite" not in leg or "paths" not in leg:
                continue
            found = files.setdefault(leg["suite"], set())
            for pattern in str(leg["paths"]).split():
                for match in _ROOT.glob(pattern):
                    found.update([match] if match.is_file() else match.rglob("*.py"))
    testing = set((_ROOT / "packages/testing").rglob("*.py"))
    for suite in plan.DAIMON:
        files.setdefault(suite, set()).update(testing)
    return files


@pytest.mark.parametrize("leaf", sorted(_LEAVES))
def test_leaf_packages_are_imported_only_by_selected_suites(leaf: str) -> None:
    pattern = re.compile(rf"^\s*(from|import)\s+{_LEAVES[leaf]}", re.M)
    offenders = sorted(
        f"{suite}: {path.relative_to(_ROOT)}"
        for suite, paths in _suite_files().items()
        if suite not in plan.SUITES[leaf]
        for path in paths
        if pattern.search(path.read_text(encoding="utf-8"))
    )
    assert not offenders


# Reads through `Path(__file__).parents[N] / "..."`, directly or through a name bound to it.
_ANCHOR = re.compile(r"^(\w+)\s*=\s*Path\(__file__\)(?:\.resolve\(\))?\.parents\[(\d+)\]\s*$", re.M)
_READ = re.compile(
    r"(?:Path\(__file__\)(?:\.resolve\(\))?\.parents\[(\d+)\]|\b(\w+))((?:\s*/\s*f?\"[^\"{]*\"?)+)"
)
_PART = re.compile(r"\"([^\"{]*)")


def _reads(path: Path) -> set[str]:
    text = path.read_text(encoding="utf-8")
    anchors = {name: int(level) for name, level in _ANCHOR.findall(text)}
    found: set[str] = set()
    for level, name, chain in _READ.findall(text):
        up = int(level) if level else anchors.get(name)
        if up is None or up >= len(path.parents):
            continue
        target = (path.parents[up] / "/".join(_PART.findall(chain))).resolve()
        if target.is_relative_to(_ROOT) and target.exists():
            found.add(target.relative_to(_ROOT).as_posix() + ("/" if target.is_dir() else ""))
    return found


def test_files_a_suite_reads_select_it() -> None:
    offenders = sorted(
        f"{suite}: {path.relative_to(_ROOT)} reads {read}"
        for suite, paths in _suite_files().items()
        for path in paths
        for read in _reads(path)
        if (name := plan.area(read)) != "all" and suite not in plan.SUITES[name]
    )
    assert not offenders


def test_deploy_without_a_base_refreshes_everything() -> None:
    full = plan.plan_deploy(None)
    assert full["full"] == "true"
    assert full["workers"] == "all"
    assert plan.plan_deploy(["Dockerfile"]) == full


def test_deploy_yml_inline_full_plan_matches() -> None:
    steps = yaml.safe_load((_ROOT / ".github/workflows/deploy.yml").read_text())["jobs"]["deploy"][
        "steps"
    ]
    run = next(step["run"] for step in steps if step.get("id") == "plan")
    printf = run[run.index("printf '%s\\n'") : run.index("| tee")].replace("\\\n", " ")
    inline = dict(arg.split("=", 1) for arg in shlex.split(printf)[2:])
    assert inline == plan.plan_deploy(None)


@pytest.mark.parametrize(
    ("paths", "expected"),
    [
        (["docs/x.md", "packages/core/tests/test_x.py", "tests/parity/test_y.py"], {
            "daimon_image": "false", "workers": "", "smoke": "false", "ssh": "false",
        }),
        (["packages/adapters/web/daimon/adapters/web/app.py", "web/src/App.tsx"], {
            "daimon_image": "true", "migrate": "false", "mcp": "false", "workers": "web",
            "reconcile": "", "smoke": "false", "ssh": "true",
        }),
        (["packages/adapters/slack/daimon/adapters/slack/app.py"], {
            "workers": "slack", "reconcile": "slack", "smoke": "true", "notebook_vm": "false",
        }),
        (["packages/adapters/mcp/daimon/adapters/mcp/server.py"], {
            "mcp": "true", "migrate": "false", "workers": "", "smoke": "true",
        }),
        (["packages/core/daimon/core/x.py"], {
            "migrate": "true", "workers": "all", "reconcile": "discord slack teams",
            "notebook_image": "false",
        }),
        (["apps/report-host/src/report_host/app.py"], {
            "report_image": "true", "notebook_image": "false", "daimon_image": "false",
            "notebook_vm": "true", "smoke": "false",
        }),
    ],
)  # fmt: skip
def test_deploy_refreshes_only_changed_components(
    paths: list[str], expected: dict[str, str]
) -> None:
    result = plan.plan_deploy(paths)
    assert result["full"] == "false"
    assert result == result | expected


def _executable(path: Path, body: str) -> None:
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(0o700)


@pytest.mark.parametrize(
    ("recorded", "have", "fetch", "base"),
    [
        ("a" * 40, True, True, "a" * 40),
        ("a" * 40, False, True, "a" * 40),
        ("a" * 40, False, False, ""),
        ("", True, True, ""),
        ("main", True, True, ""),
        ("a" * 40 + "; true", True, True, ""),
    ],
)
def test_deploy_plan_falls_back_to_a_full_deploy(
    tmp_path: Path, recorded: str, have: bool, fetch: bool, base: str
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _executable(bin_dir / "gcloud", f"printf '%s\\n' {shlex.quote(recorded)}\n")
    _executable(
        bin_dir / "git",
        f'case "$1" in cat-file) exit {0 if have else 1} ;; fetch) exit {0 if fetch else 1} ;; esac\n',
    )
    _executable(bin_dir / "python3", 'printf "%s\\n" "$@"\n')
    result = subprocess.run(
        ["bash", str(_ROOT / "scripts/ci/deploy_plan.sh"), "vm", "zone"],
        env={**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    *notice, script, mode, flag, passed = result.stdout.splitlines()
    assert [script, mode, flag, passed] == ["scripts/ci/plan.py", "deploy", "--base", base]
    assert bool(notice) is not bool(base)
