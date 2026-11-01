#!/usr/bin/env python3
"""Which CI jobs and legs a change needs, and which staging components a deploy refreshes.

Each changed path maps to one area: FILES by exact path, then prose (*.md outside
the code trees), then the first prefix in RULES that matches. A path nothing
names maps to "all", so a new top-level file fails safe.
Stdlib only: it runs before any setup step.

    plan.py tests [--base REV]   a pull request; no REV selects every job
    plan.py deploy [--base REV]  REV is the deployed commit; none or unreadable deploys all

Writes key=value lines to $GITHUB_OUTPUT when it is set, else to stdout.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from collections.abc import Iterable, Mapping
from typing import Any

#: Files outside the code that tests read or lint regenerates and compares.
FILES: dict[str, str] = {
    ".env.example": "generated",
    "docs/configuration.md": "generated",
    "docs/mcp-tools.md": "mcp-catalogue",
    "docs/slack-app-manifest.yaml": "slack-manifest",
    "docs/teams-app-manifest.yaml": "teams-manifest",
    "docker-compose.yml": "docs",
}
#: Prose no test or image reads; code trees keep their own (skills, fixtures).
_PROSE = re.compile(r"^(?!defaults/|packages/|tests/|qa/)\S*\.md$")
#: First match wins, so narrower prefixes come first.
RULES: tuple[tuple[str, str], ...] = (
    ("docs/", "docs"),
    ("mkdocs.yml", "docs"),
    ("web/", "spa"),
    ("tests/parity/", "parity"),
    ("tests/harness_conformance/", "harness"),
    ("tests/golden/", "golden"),
    ("tests/", "tests"),
    ("qa/", "tests"),
    ("packages/mux/", "mux"),
    ("packages/adapters/web/", "web"),
    ("packages/adapters/discord/", "discord"),
    ("packages/adapters/slack/", "slack"),
    ("packages/adapters/teams/", "teams"),
    ("packages/adapters/mcp/", "mcp"),
    ("packages/adapters/scheduler/", "scheduler"),
    # Every pytest run loads its stop_leg_default plugin (pyproject addopts).
    ("packages/testing/", "testing"),
    # Core and the CLI, which migrations and the smoke turn run through.
    ("packages/", "core"),
    ("defaults/", "core"),
    ("alembic.ini", "core"),
    ("docker/", "core"),
    ("assets/", "core"),
    ("apps/harness/", "harness"),
    ("apps/notebook-host/", "notebook-host"),
    ("apps/report-host/", "report-host"),
    ("scripts/check_report_host_lock.py", "report-host"),
    # The Discord card tests replay this model.
    ("formal/", "formal"),
)

#: The suites of every job that tests the daimon packages. Their shared helpers
#: (daimon.testing) import every adapter but web, so those adapters select them all.
DAIMON = (
    "core", "mcp", "slack", "teams", "web", "parity", "discord",
    "turn-mux", "parity-mux", "slack-mux", "discord-mux", "teams-mux",
    "scheduler-mux", "cli-run-mux", "nonchat-mux",
    "staging-config", "journeys", "lint", "shard-check",
)  # fmt: skip
_HOSTS = (*DAIMON, "image-daimon")

#: Suites (matrix `suite` values, or the job's own key) each area needs.
#: tests/test_ci_plan.py checks the leaf areas against what the tests import.
SUITES: dict[str, tuple[str, ...]] = {
    "docs": (),
    "generated": ("lint",),
    "mcp-catalogue": ("lint", "core", "mux"),
    "slack-manifest": ("slack", "slack-mux", "parity", "parity-mux"),
    "teams-manifest": ("teams", "teams-mux", "parity", "parity-mux"),
    "formal": ("discord", "discord-mux", "lint"),
    # tests/parity reads web/src for the plain-words rules.
    "spa": ("spa", "parity", "parity-mux", "image-daimon"),
    "web": ("web", "parity", "parity-mux", "lint", "shard-check", "image-daimon"),
    "parity": ("parity", "parity-mux", "staging-config", "lint", "shard-check"),
    "tests": DAIMON,
    # The mux tests read the golden scenarios, defaults/ (core) and the agent-chat tool (mcp).
    "golden": (*DAIMON, "mux"),
    "testing": (*DAIMON, "mux", "harness", "harness-conformance", "report-host", "notebook-host"),
    "core": (*_HOSTS, "mux"),
    "discord": _HOSTS,
    "slack": _HOSTS,
    "teams": _HOSTS,
    "mcp": (*_HOSTS, "mux"),
    "scheduler": _HOSTS,
    "mux": (*_HOSTS, "mux", "harness", "harness-conformance", "image-harness"),
    # The mux tests drive the harness service.
    "harness": ("harness", "harness-conformance", "mux", "image-harness", "lint"),
    # Core's tests and daimon.testing import both hosts.
    "notebook-host": (*DAIMON, "notebook-host", "image-notebook-host"),
    "report-host": (*DAIMON, "report-host", "image-report-host"),
}

Leg = dict[str, str | int]


def _shards(suite: str, count: int, paths: str, **extra: str | int) -> list[Leg]:
    return [
        {"suite": suite, "shard": i, "shards": count, **extra, "paths": paths} for i in range(count)
    ]


_CORE = (
    "qa/live/tests packages/core packages/adapters/cli packages/adapters/scheduler packages/testing"
    " tests/integration tests/platform_contract tests/test_*.py tests/performance"
)
_NONCHAT = " ".join(
    [
        *(f"packages/core/tests/test_{name}.py" for name in (
            "headless_runner", "headless_ports", "headless_tool_safety",
            "routine_delivery_headless", "scheduler_dispatch", "smoke", "workspace_transfer",
            "transfer_checkpoint_paths", "session_preparation", "channel_backend", "shared_threads",
        )),
        "tests/integration/test_routines_end_to_end.py",
        "tests/integration/test_direct_messages.py",
    ]
)  # fmt: skip
#: The legs of the ci.yml jobs whose matrix the plan fills in: a pull request gets only
#: the legs of its selected suites, so an unselected leg never starts. Add a shard here.
MATRICES: dict[str, list[Leg]] = {
    "pytest-db": [
        *_shards("core", 6, _CORE, **{"fetch-depth": 0}),
        *_shards("mcp", 2, "packages/adapters/mcp", **{"fetch-depth": 1}),
        *(
            leg
            for suite, paths in (
                ("slack", "packages/adapters/slack"),
                ("teams", "packages/adapters/teams"),
                ("web", "packages/adapters/web"),
                ("parity", "tests/parity"),
            )
            for leg in _shards(suite, 1, paths, **{"fetch-depth": 1})
        ),
    ],
    "pytest-mux-leg": [
        *_shards("turn-mux", 1, "packages/core/tests/turn"),
        *_shards("parity-mux", 1, "tests/parity"),
        *_shards("slack-mux", 1, "packages/adapters/slack"),
        *_shards("discord-mux", 2, "packages/adapters/discord"),
        *_shards("teams-mux", 1, "packages/adapters/teams"),
        *_shards("scheduler-mux", 1, "packages/adapters/scheduler"),
        *_shards("cli-run-mux", 1, "packages/adapters/cli/tests/run"),
        *_shards("nonchat-mux", 1, _NONCHAT),
    ],
    "pytest-light": [
        {"suite": "report-host", "paths": "apps/report-host"},
        {"suite": "harness", "paths": "apps/harness"},
        {"suite": "mux", "paths": "packages/mux"},
        {"suite": "harness-conformance", "paths": "tests/harness_conformance"},
    ],
}

#: Every ci.yml job that can be skipped whole, with the suites it runs.
#: journeys-offline is a required check, so it always runs and gates its steps.
JOBS: dict[str, tuple[str, ...]] = {
    "lint": ("lint",),
    "web": ("spa",),
    **{
        job: tuple(dict.fromkeys(str(leg["suite"]) for leg in legs))
        for job, legs in MATRICES.items()
    },
    "pytest-staging-config": ("staging-config",),
    "pytest-discord": ("discord",),
    "mux-standalone": ("mux",),
    "pytest-notebook-host": ("notebook-host",),
    "shard-check": ("shard-check",),
    "docker-build": ("image-daimon", "image-notebook-host", "image-report-host"),
    "harness-image": ("image-harness",),
}
ALL_SUITES = tuple(sorted({s for suites in (*SUITES.values(), *JOBS.values()) for s in suites}))

#: Worker VM compose services one area's change can restart on its own.
SERVICES = ("discord", "slack", "teams", "scheduler", "web")
#: Staging components each area changes. "all" in workers means every compose service.
COMPONENTS: dict[str, tuple[str, ...]] = {
    "core": ("daimon", "migrate", "mcp", "workers:all"),
    "mux": ("daimon", "migrate", "mcp", "workers:all"),
    "discord": ("daimon", "workers:discord"),
    "slack": ("daimon", "workers:slack"),
    "teams": ("daimon", "workers:teams"),
    "scheduler": ("daimon", "workers:scheduler"),
    "web": ("daimon", "workers:web"),
    "spa": ("daimon", "workers:web"),
    "mcp": ("daimon", "mcp"),
    "notebook-host": ("notebook",),
    "report-host": ("report",),
}
_JOURNEY = re.compile(r"(^|/)test_j[0-9]+_[^/]*\.py$")


def area(path: str) -> str:
    if path in FILES:
        return FILES[path]
    if _PROSE.match(path):
        return "docs"
    return next((name for prefix, name in RULES if path.startswith(prefix)), "all")


def changed_paths(base: str | None, head: str) -> list[str] | None:
    """Paths that differ between two trees; None when either is unreadable."""
    if not base:
        return None
    result = subprocess.run(
        ["git", "diff", "--name-only", "--no-renames", base, head],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        print(f"plan: cannot diff {base}..{head}: {result.stderr.strip()}", file=sys.stderr)
        return None
    return [line for line in result.stdout.splitlines() if line]


def plan_tests(paths: Iterable[str] | None) -> dict[str, str]:
    suites: set[str] = set()
    for path in paths or ():
        name = area(path)
        suites.update(ALL_SUITES if name == "all" else SUITES[name])
        if _JOURNEY.search(path):
            suites.add("journeys")
    if paths is None:
        suites.update(ALL_SUITES)
    jobs = sorted(job for job, owned in JOBS.items() if suites & set(owned))
    matrices = {
        job: json.dumps({"include": [leg for leg in legs if leg["suite"] in suites]})
        for job, legs in MATRICES.items()
    }
    return {"suites": json.dumps(sorted(suites)), "jobs": json.dumps(jobs), **matrices}


def matrix_legs(job: Mapping[str, Any]) -> list[Leg]:
    """A ci.yml job's legs: its matrix, or the MATRICES entry its matrix expression names."""
    matrix = job.get("strategy", {}).get("matrix", {})
    if not isinstance(matrix, str):
        return list(matrix.get("include", []))
    planned = re.search(r"needs\.changes\.outputs\.([\w-]+)", matrix)
    if not planned:
        raise ValueError(f"unknown matrix expression: {matrix}")
    return list(MATRICES[planned[1]])


def _shipped(path: str) -> bool:
    """False for test files, which no image copies (.dockerignore, app Dockerfiles)."""
    return not (path.startswith(("packages/", "apps/")) and "/tests/" in path)


def plan_deploy(paths: Iterable[str] | None) -> dict[str, str]:
    paths = None if paths is None else list(paths)
    full = paths is None or any(area(path) == "all" for path in paths)
    if paths is None or full:
        parts = {"daimon", "notebook", "report", "migrate", "mcp", "workers:all"}
    else:
        parts = {
            part for path in paths if _shipped(path) for part in COMPONENTS.get(area(path), ())
        }
    every = "workers:all" in parts
    refreshed = set(SERVICES) if every else {s for s in SERVICES if f"workers:{s}" in parts}
    turn = {"migrate", "mcp"} & parts or {"discord", "slack", "teams", "scheduler"} & refreshed

    def flag(value: object) -> str:
        return "true" if value else "false"

    notebook_vm = {"notebook", "report"} & parts
    return {
        "full": flag(full),
        "daimon_image": flag("daimon" in parts),
        "notebook_image": flag("notebook" in parts),
        "report_image": flag("report" in parts),
        "migrate": flag("migrate" in parts),
        "mcp": flag("mcp" in parts),
        "workers": "all" if every else " ".join(s for s in SERVICES if s in refreshed),
        "reconcile": " ".join(s for s in ("discord", "slack", "teams") if s in refreshed),
        "smoke": flag(turn),
        "notebook_vm": flag(notebook_vm),
        "ssh": flag(refreshed or notebook_vm or turn),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("mode", choices=("tests", "deploy"))
    parser.add_argument("--base", default="")
    parser.add_argument("--head", default="HEAD")
    args = parser.parse_args(argv)
    paths = changed_paths(args.base, args.head)
    if paths is not None:
        areas = " ".join(sorted({area(path) for path in paths})) or "none"
        print(f"plan: {len(paths)} changed paths, areas: {areas}", file=sys.stderr)
    else:
        print("plan: no comparable base, selecting everything", file=sys.stderr)
    plan = plan_tests(paths) if args.mode == "tests" else plan_deploy(paths)
    lines = "".join(f"{key}={value}\n" for key, value in plan.items())
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with open(output, "a", encoding="utf-8") as handle:
            handle.write(lines)
    print(lines, end="", file=sys.stderr if output else sys.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
