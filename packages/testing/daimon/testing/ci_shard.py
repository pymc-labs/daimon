"""Stable pytest collection sharding for CI jobs.

Each shard keeps its own node IDs.  This works with xdist because the
controller and workers all receive the same options.

``--ci-shard-by=test`` (the default) hashes every node ID, so each shard
collects the whole suite and then deselects.

``--ci-shard-by=file`` places whole test files instead: a shard never imports
the modules of the other shards (``pytest_ignore_collect`` skips them while
pytest walks a directory), and each xdist worker collects roughly ``1/count``
of the suite instead of all of it. Where a file goes depends only on its path,
the shard count and ``--ci-shard-suite`` -- never on what else was collected --
so every shard, every xdist worker and the CI completeness check agree:

* a file listed under the suite in ``ci_durations.json`` (regenerate it with
  ``scripts/generate_ci_durations.py``) goes where a greedy longest-first
  assignment of the listed files puts it: the next file, heaviest first, joins
  the least-loaded shard, ties broken by shard index and then by path;
* any other file (new since the table was made) goes to the shard its path
  hashes to.  It lands in exactly one shard; the table only has to be
  refreshed to rebalance, never to keep tests from running.
"""

import hashlib
import json
from functools import cache
from pathlib import Path

import pytest

DURATIONS = Path(__file__).with_name("ci_durations.json")


def shard_for(nodeid: str, count: int) -> int:
    return int.from_bytes(hashlib.sha256(nodeid.encode()).digest()[:8], "big") % count


def _file_key(nodeid: str) -> str:
    return nodeid.partition("::")[0]


@cache
def _load(table: Path, suite: str) -> dict[str, float]:
    if not table.is_file():
        return {}
    suites: dict[str, dict[str, float]] = json.loads(table.read_text())
    return suites.get(suite, {})


@cache
def _balanced(table: Path, suite: str, count: int) -> dict[str, int]:
    """Greedy longest-first placement of the table's files on ``count`` shards."""
    loads = [0.0] * count
    placed: dict[str, int] = {}
    for path, seconds in sorted(_load(table, suite).items(), key=lambda kv: (-kv[1], kv[0])):
        shard = min(range(count), key=lambda index: (loads[index], index))
        loads[shard] += seconds
        placed[path] = shard
    return placed


def file_shard(path: str, count: int, suite: str | None = None, table: Path = DURATIONS) -> int:
    """The shard of one test file (a path relative to the pytest rootdir)."""
    if suite is not None:
        placed = _balanced(table, suite, count).get(path)
        if placed is not None:
            return placed
    return shard_for(path, count)


def projected_loads(count: int, suite: str, table: Path = DURATIONS) -> list[float]:
    """Seconds per shard if every table file ran where ``file_shard`` puts it."""
    loads = [0.0] * count
    for path, seconds in _load(table, suite).items():
        loads[file_shard(path, count, suite, table)] += seconds
    return loads


def _shard_of(item_nodeid: str, config: pytest.Config, count: int) -> int:
    if config.getoption("ci_shard_by") == "file":
        return file_shard(
            _file_key(item_nodeid),
            count,
            config.getoption("ci_shard_suite"),
            config.getoption("ci_shard_durations") or DURATIONS,
        )
    return shard_for(item_nodeid, count)


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("ci-shard")
    group.addoption("--ci-shard-index", type=int, default=None)
    group.addoption("--ci-shard-count", type=int, default=None)
    group.addoption("--ci-shard-by", choices=("test", "file"), default="test")
    group.addoption("--ci-shard-suite", default=None, help="section of ci_durations.json to use")
    group.addoption("--ci-shard-durations", type=Path, default=None, help="alternative table")


def pytest_ignore_collect(collection_path: Path, config: pytest.Config) -> bool | None:
    """Skip other shards' test files before they are imported (file mode only).

    pytest does not call this for a file named on the command line; those are
    still filtered by ``pytest_collection_modifyitems`` below, which stays the
    authority on what runs.
    """
    index = config.getoption("ci_shard_index")
    count = config.getoption("ci_shard_count")
    if (
        index is None
        or count is None
        or config.getoption("ci_shard_by") != "file"
        or not collection_path.is_file()
        or collection_path.suffix != ".py"
        or not collection_path.name.startswith("test_")
    ):
        return None
    try:
        relative = collection_path.relative_to(config.rootpath)
    except ValueError:
        return None
    shard = file_shard(
        relative.as_posix(),
        count,
        config.getoption("ci_shard_suite"),
        config.getoption("ci_shard_durations") or DURATIONS,
    )
    return True if shard != index else None


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    index = config.getoption("ci_shard_index")
    count = config.getoption("ci_shard_count")
    if index is None and count is None:
        return
    if index is None or count is None or count < 1 or not 0 <= index < count:
        raise pytest.UsageError("--ci-shard-index and --ci-shard-count require 0 <= index < count")

    kept = [item for item in items if _shard_of(item.nodeid, config, count) == index]
    deselected = [item for item in items if _shard_of(item.nodeid, config, count) != index]
    config.hook.pytest_deselected(items=deselected)
    items[:] = kept
