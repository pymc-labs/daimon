"""Prove the CI pytest shards cover each suite exactly once.

Reads the matrix of every job in ``.github/workflows/ci.yml`` whose pytest step
loads the plugin (``-p daimon.testing.ci_shard``: today ``pytest-db`` and
``pytest-discord``). For every suite it collects the matrix paths once,
unsharded and with ``--collect-only`` (no database, nothing runs), then places
each collected test file with ``daimon.testing.ci_shard.file_shard`` -- the
function the plugin applies in CI, which depends only on a file's path, the
shard count and the suite -- and fails when

* the legs are not shards ``0..N-1`` of one ``shards`` count over the same
  paths, a suite has legs in more than one job, or a job's pytest command does
  not pass the shard options the check assumes (``--ci-shard-by=file`` and the
  matrix's suite, index and count),
* a shard would receive no test file (pytest exits 5 on it and the job fails),
* the shards do not hold exactly the collected tests, or a file is placed
  outside ``0..N-1``.

Suites with one shard are not split, so they are not collected at all.
One collection per suite keeps this cheap; the plugin's behaviour under real
pytest is proved on a synthetic tree by ``packages/testing/tests/test_ci_shard.py``.
It prints each shard's test count and its projected seconds from the duration
table, and notes table entries for files that no longer exist.
"""

from __future__ import annotations

import argparse
import glob
import re
import shlex
import subprocess
import sys
from collections import Counter
from pathlib import Path
from typing import Any, cast

import yaml
from daimon.testing.ci_shard import DURATIONS, file_shard, projected_loads

_WORKFLOW = Path(".github/workflows/ci.yml")
_PLUGIN = "-p daimon.testing.ci_shard"
_NODEID = re.compile(r"^(.+?\.py)::")
_REQUIRED_OPTIONS = (
    _PLUGIN,
    "--ci-shard-by=file",
    "--ci-shard-suite=${{ matrix.suite }}",
    "--ci-shard-index=${{ matrix.shard }}",
    "--ci-shard-count=${{ matrix.shards }}",
)


def _pytest_commands(job: dict[str, Any]) -> list[str]:
    return [
        " ".join(step["run"].split())
        for step in cast("list[dict[str, Any]]", job.get("steps", []))
        if "uv run pytest" in step.get("run", "")
    ]


def sharded_jobs(workflow: Path) -> dict[str, dict[str, Any]]:
    """The jobs whose pytest step loads the shard plugin, by job id."""
    document = cast("dict[str, Any]", yaml.safe_load(workflow.read_text()))
    jobs = cast("dict[str, dict[str, Any]]", document["jobs"])
    return {
        name: job
        for name, job in jobs.items()
        if any(_PLUGIN in command for command in _pytest_commands(job))
    }


def _legs(jobs: dict[str, dict[str, Any]]) -> tuple[dict[str, list[dict[str, Any]]], list[str]]:
    """Matrix legs of the sharded jobs grouped by suite, and suites split across jobs."""
    suites: dict[str, list[dict[str, Any]]] = {}
    owner: dict[str, str] = {}
    problems: list[str] = []
    for name, job in jobs.items():
        for leg in cast("list[dict[str, Any]]", job["strategy"]["matrix"]["include"]):
            suite = str(leg["suite"])
            if owner.setdefault(suite, name) != name:
                problems.append(f"{suite}: legs in both {owner[suite]} and {name}")
            suites.setdefault(suite, []).append(leg)
    return suites, sorted(set(problems))


def _command_problems(name: str, job: dict[str, Any]) -> list[str]:
    commands = _pytest_commands(job)
    if len(commands) != 1:
        return [f"{name} must have exactly one pytest step, found {len(commands)}"]
    return [
        f"the {name} pytest command lacks `{option}`"
        for option in _REQUIRED_OPTIONS
        if option not in commands[0]
    ]


def _expand(paths: str) -> list[str]:
    """Expand globs the way the CI shell does, keeping literal paths as they are."""
    expanded: list[str] = []
    for token in shlex.split(paths):
        expanded.extend(sorted(glob.glob(token)) or [token])
    return expanded


def _collect(paths: list[str]) -> list[str]:
    command = [sys.executable, "-m", "pytest", *paths, "--collect-only", "-q"]
    command += ["-p", "no:cacheprovider"]
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        sys.stderr.write(result.stdout + result.stderr)
        raise SystemExit(f"collection failed ({' '.join(command[3:])})")
    # `-q` prints one node id per line (parameter ids may contain spaces); warnings,
    # summaries and tracebacks around them never start with `<relative path>.py::`.
    return [line for line in result.stdout.splitlines() if _NODEID.match(line)]


def check_suite(suite: str, legs: list[dict[str, Any]]) -> list[str]:
    """Return the problems found for one suite (empty when the shards are exact)."""
    counts = {int(leg["shards"]) for leg in legs}
    if len(counts) != 1:
        return [f"{suite}: legs disagree on `shards`: {sorted(counts)}"]
    count = counts.pop()
    problems: list[str] = []
    if len({str(leg["paths"]) for leg in legs}) != 1:
        problems.append(f"{suite}: legs select different paths")
    if sorted(int(leg["shard"]) for leg in legs) != list(range(count)):
        problems.append(f"{suite}: legs must be shards 0..{count - 1}, once each")

    if count == 1:
        # One shard is the whole suite by construction; nothing to split or lose.
        print(f"{suite}: one shard, not split")
        return problems

    nodeids = Counter(_collect(_expand(str(legs[0]["paths"]))))
    per_file = Counter(nodeid.partition("::")[0] for nodeid in nodeids.elements())
    shard_of = {file: file_shard(file, count, suite) for file in per_file}
    tests = [0] * count
    files = [0] * count
    for file, tests_in_file in per_file.items():
        tests[shard_of[file]] += tests_in_file
        files[shard_of[file]] += 1

    seconds = projected_loads(count, suite)
    print(
        f"{suite}: {sum(nodeids.values())} tests in {len(per_file)} files; "
        f"shard tests {' + '.join(map(str, tests))}; "
        f"projected seconds {' / '.join(f'{s:.0f}' for s in seconds)}"
    )
    if not nodeids:
        problems.append(f"{suite}: collection is empty")
    problems += [
        f"{suite}: shard {i} would receive no test file" for i in range(count) if not files[i]
    ]
    problems += [
        f"{suite}: {file} is placed on shard {shard} outside 0..{count - 1}"
        for file, shard in sorted(shard_of.items())
        if not 0 <= shard < count
    ]
    if sum(tests) != sum(nodeids.values()):
        problems.append(
            f"{suite}: shards hold {sum(tests)} tests, the suite has {sum(nodeids.values())}"
        )
    return problems


def _stale_entries(suites: list[str]) -> list[str]:
    import json

    table = cast("dict[str, dict[str, float]]", json.loads(DURATIONS.read_text()))
    return [
        f"{suite}: {file}"
        for suite in suites
        for file in sorted(table.get(suite, {}))
        if not Path(file).is_file()
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workflow", type=Path, default=_WORKFLOW)
    parser.add_argument("suites", nargs="*", help="check only these suites (default: all)")
    args = parser.parse_args()

    jobs = sharded_jobs(args.workflow)
    if not jobs:
        raise SystemExit(f"no job in {args.workflow} loads the shard plugin ({_PLUGIN})")
    suites, problems = _legs(jobs)
    selected = args.suites or list(suites)
    unknown = [name for name in selected if name not in suites]
    if unknown:
        raise SystemExit(f"unknown suite(s) {unknown}; the matrices have {list(suites)}")
    problems += [problem for name, job in jobs.items() for problem in _command_problems(name, job)]
    problems += [problem for name in selected for problem in check_suite(name, suites[name])]
    stale = _stale_entries(selected)
    if stale:
        print(
            f"note: {len(stale)} ci_durations.json entries name files that no longer exist "
            "(harmless; regenerate with scripts/generate_ci_durations.py)"
        )
    for problem in problems:
        print(f"ERROR {problem}", file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
