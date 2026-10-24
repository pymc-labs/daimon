"""Ratchet for the mux CI leg: known failures are strict expected failures.

Loaded only by the mux-leg CI command (``-p daimon.testing.mux_leg_known``). It
reads ``tests/mux_leg_known_failures.txt`` (override with
``--mux-leg-known-failures``), one pytest node id per line, ``#`` for comments,
and marks exactly those tests ``xfail(strict=True)``. The leg therefore

* fails on any failure that is not listed (a new regression),
* fails when a listed test starts passing, so the list can only shrink, and
* fails when a listed id no longer exists in a collected file (renamed or
  deleted), so the list cannot rot.
"""

from __future__ import annotations

from pathlib import Path

import pytest

DEFAULT_LIST = Path("tests/mux_leg_known_failures.txt")
REASON = "known mux-leg failure (tests/mux_leg_known_failures.txt); remove the line once it passes"


def read_known(path: Path) -> list[str]:
    """Node ids of a list file, without blank lines and comments."""
    lines = (line.partition("#")[0].strip() for line in path.read_text().splitlines())
    return [line for line in lines if line]


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.getgroup("mux-leg").addoption(
        "--mux-leg-known-failures", type=Path, default=None, help="list of known mux-leg failures"
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    option: Path | None = config.getoption("mux_leg_known_failures")
    path = option if option is not None else config.rootpath / DEFAULT_LIST
    known = set(read_known(path))
    collected = {item.nodeid for item in items}
    for item in items:
        if item.nodeid in known:
            item.add_marker(pytest.mark.xfail(strict=True, reason=REASON))
    collected_files = {nodeid.partition("::")[0] for nodeid in collected}
    stale = sorted(
        nodeid
        for nodeid in known - collected
        # A deleted or renamed file is stale on every shard. A missing test in a file
        # this run collected is stale too; other shards' files are theirs to check.
        if not (config.rootpath / nodeid.partition("::")[0]).is_file()
        or (nodeid.partition("::")[0] in collected_files and not config.option.deselect)
    )
    # `-k` narrows collection on purpose; the root `-m` default filter only drops opt-in
    # live/slow markers, which no listed test carries, so it does not disable the check.
    if stale and not config.getoption("keyword"):
        raise pytest.UsageError(
            f"{path} lists tests that no longer exist: {stale[:5]} ({len(stale)} in total)"
        )
