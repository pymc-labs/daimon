"""Stable pytest collection sharding for CI jobs.

Each shard collects the same suite, then keeps its own node IDs.  This works
with xdist because the controller and workers all receive the same options.
"""

import hashlib

import pytest


def shard_for(nodeid: str, count: int) -> int:
    return int.from_bytes(hashlib.sha256(nodeid.encode()).digest()[:8], "big") % count


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("ci-shard")
    group.addoption("--ci-shard-index", type=int, default=None)
    group.addoption("--ci-shard-count", type=int, default=None)


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    index = config.getoption("ci_shard_index")
    count = config.getoption("ci_shard_count")
    if index is None and count is None:
        return
    if index is None or count is None or count < 1 or not 0 <= index < count:
        raise pytest.UsageError("--ci-shard-index and --ci-shard-count require 0 <= index < count")

    kept = [item for item in items if shard_for(item.nodeid, count) == index]
    deselected = [item for item in items if shard_for(item.nodeid, count) != index]
    config.hook.pytest_deselected(items=deselected)
    items[:] = kept
