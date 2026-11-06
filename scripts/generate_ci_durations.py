"""Regenerate the per-file duration table that balances the CI pytest shards.

``daimon.testing.ci_shard`` places each test file on a shard greedily by the
seconds recorded in ``packages/testing/daimon/testing/ci_durations.json``. This
script rebuilds that table from junit XML written by CI (``--junitxml``), either
from files you already have or from the artifacts of a workflow run:

    uv run python scripts/generate_ci_durations.py path/to/junit-dir-or-file ...
    uv run python scripts/generate_ci_durations.py --run 123456789

Only the suites CI splits over more than one shard read the table, so only they
are written. The suite a junit file belongs to is the longest suite name of the
sharded CI jobs that its name equals or starts with followed by ``-``
(``discord-mux-shard-1.xml`` -> ``discord-mux``, ``core-shard-2.xml`` -> ``core``,
as CI names them); files of other suites are skipped. ``--suite`` overrides it
and writes that suite whatever its shard count. A file's seconds are the sum of
its test cases' ``time`` (setup, call and teardown). The junit files of one
suite are read as the shards of one run, so their cases add up; a case present
in more than one (repeated runs, or runs sharded differently) takes the median
of its timings. Suites not present in the input keep their current entries, so
refresh one suite at a time. Reading a run uses ``gh run download`` only, which
is read-only.

Junit records a dotted ``classname``, not a path, so each is resolved to the
longest prefix that is a ``.py`` file under the current directory; test cases
that resolve to no file are counted and ignored.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import statistics
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from collections import defaultdict
from collections.abc import Iterable
from pathlib import Path

TABLE = Path("packages/testing/daimon/testing/ci_durations.json")


def _sharded_suites() -> dict[str, int]:
    """Shard count per suite of the CI jobs that load the shard plugin."""
    path = Path(__file__).with_name("check_ci_shards.py")
    spec = importlib.util.spec_from_file_location("check_ci_shards", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.sharded_suites()


def suite_of(stem: str, suites: Iterable[str]) -> str | None:
    """The longest suite name ``stem`` equals or starts with followed by ``-``."""
    return next(
        (
            suite
            for suite in sorted(suites, key=len, reverse=True)
            if stem == suite or stem.startswith(f"{suite}-")
        ),
        None,
    )


def _resolve(classname: str, root: Path, cache: dict[str, str | None]) -> str | None:
    if classname not in cache:
        parts = classname.split(".")
        cache[classname] = next(
            (
                "/".join(parts[:end]) + ".py"
                for end in range(len(parts), 0, -1)
                if (root / ("/".join(parts[:end]) + ".py")).is_file()
            ),
            None,
        )
    return cache[classname]


def read_junit(path: Path) -> dict[tuple[str, str], float]:
    """Seconds per (classname, test name) in one junit file."""
    return {
        (case.get("classname", ""), case.get("name", "")): float(case.get("time", "0"))
        for case in ET.parse(path).getroot().iter("testcase")
    }


def _junit_files(inputs: list[Path]) -> list[Path]:
    files: list[Path] = []
    for path in inputs:
        files += sorted(path.rglob("*.xml")) if path.is_dir() else [path]
    return files


def build(
    files: list[Path], root: Path, suite_override: str | None, shards: dict[str, int]
) -> tuple[dict[str, dict[str, float]], int]:
    """Suite -> file -> seconds, merged across junit files, plus unresolved case count.

    ``shards`` maps each suite name to its shard count; without ``suite_override``
    a junit file counts only when its suite has more than one shard.
    """
    samples: dict[str, dict[tuple[str, str], list[float]]] = defaultdict(lambda: defaultdict(list))
    for path in files:
        suite = suite_override or suite_of(path.stem, shards)
        if suite is None or (suite_override is None and shards[suite] < 2):
            print(f"skipped {path}: not a sharded suite", file=sys.stderr)
            continue
        for key, value in read_junit(path).items():
            samples[suite][key].append(value)
    cache: dict[str, str | None] = {}
    table: dict[str, dict[str, float]] = {}
    unresolved = 0
    for suite, cases in sorted(samples.items()):
        seconds: dict[str, float] = defaultdict(float)
        for (classname, _), values in cases.items():
            file = _resolve(classname, root, cache)
            if file is None:
                unresolved += 1
            else:
                seconds[file] += statistics.median(values)
        table[suite] = {file: round(value, 2) for file, value in sorted(seconds.items())}
    return table, unresolved


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument(
        "junit", nargs="*", type=Path, help="junit XML files or directories of them"
    )
    parser.add_argument("--run", help="download the *-junit artifacts of this workflow run id")
    parser.add_argument("--repo", help="owner/name for --run (default: the current repository)")
    parser.add_argument("--suite", help="suite name for every input (default: from file names)")
    parser.add_argument("--table", type=Path, default=TABLE)
    args = parser.parse_args()

    with tempfile.TemporaryDirectory() as download:
        inputs: list[Path] = list(args.junit)
        if args.run:
            command = ["gh", "run", "download", args.run, "--dir", download, "--pattern", "*-junit"]
            if args.repo:
                command += ["--repo", args.repo]
            subprocess.run(command, check=True)
            inputs.append(Path(download))
        files = _junit_files(inputs)
        if not files:
            parser.error("no junit files: pass paths or --run")
        fresh, unresolved = build(files, Path.cwd(), args.suite, _sharded_suites())

    table: dict[str, dict[str, float]] = (
        json.loads(args.table.read_text()) if args.table.exists() else {}
    )
    table.update(fresh)
    args.table.write_text(json.dumps(table, indent=1, sort_keys=True) + "\n")
    for suite, by_file in fresh.items():
        print(f"{suite}: {len(by_file)} files, {sum(by_file.values()):.0f} s total")
    if unresolved:
        print(f"ignored {unresolved} test cases whose classname is not a file", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
