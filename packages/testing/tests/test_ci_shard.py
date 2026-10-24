import json
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import pytest
from daimon.testing.ci_shard import file_shard, projected_loads, shard_for


@pytest.mark.parametrize("count", [3, 4])
def test_shards_partition_collection_with_duplicates_preserved(count: int) -> None:
    nodeids = [f"suite/test_example.py::test_case[{index}]" for index in range(130)]
    nodeids += [nodeids[17], nodeids[17]]

    partitions = [
        [nodeid for nodeid in nodeids if shard_for(nodeid, count) == shard]
        for shard in range(count)
    ]

    assert Counter(nodeid for partition in partitions for nodeid in partition) == Counter(nodeids)
    assert all(partitions)
    assert partitions == [
        [nodeid for nodeid in nodeids if shard_for(nodeid, count) == shard]
        for shard in range(count)
    ]


@pytest.mark.parametrize("count", [1, 2, 3, 4, 7])
def test_shard_is_in_range(count: int) -> None:
    assert all(0 <= shard_for(f"test_{i}", count) < count for i in range(100))


def test_file_mode_keeps_every_test_of_a_file_together() -> None:
    from daimon.testing.ci_shard import _file_key  # pyright: ignore[reportPrivateUsage]

    nodeids = [
        f"suite/test_{module}.py::test_case[{case}]" for module in "abcdefgh" for case in range(20)
    ]
    shards = {
        module: {shard_for(_file_key(n), 4) for n in nodeids if f"test_{module}.py" in n}
        for module in "abcdefgh"
    }
    assert all(len(owners) == 1 for owners in shards.values())
    assert len({next(iter(owners)) for owners in shards.values()}) > 1


def _collect(root: Path, *extra: str) -> list[str]:
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "suite", "test_top.py", "--collect-only", "-q"]
        + ["-p", "no:cacheprovider", "-p", "daimon.testing.ci_shard", *extra],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode in (0, 5), result.stdout + result.stderr  # 5: empty shard
    return [line for line in result.stdout.splitlines() if "::" in line]


@dataclass(frozen=True)
class ShardSuite:
    """Eight test files of five cases each, plus a top-level file, and what they collect to."""

    root: Path
    imported: Path  # a file per suite module the collecting process imported
    durations: Path  # a table that knows a..c; d..h are new since it was made
    whole: list[str]


@pytest.fixture(scope="module")
def shard_suite(tmp_path_factory: pytest.TempPathFactory) -> ShardSuite:
    """Build the suite once and collect it unsharded once for every shard count and mode.

    Each collection is a fresh pytest process, because the proof is about which files a
    process imports; the unsharded one is the same for every parametrization.
    """
    root = tmp_path_factory.mktemp("shard_suite")
    suite = root / "suite"
    suite.mkdir()
    imported = root / "imported"
    imported.mkdir()
    for module in "abcdefgh":
        (suite / f"test_{module}.py").write_text(
            "import pathlib, pytest\n"
            f"pathlib.Path({str(imported)!r}, {module!r}).touch()\n"
            "@pytest.mark.parametrize('n', range(5))\n"
            "def test_case(n): pass\n"
        )
    (root / "test_top.py").write_text("def test_top(): pass\n")
    durations = root / "durations.json"
    known = {"suite/test_a.py": 9.0, "suite/test_b.py": 7.0, "suite/test_c.py": 3.0}
    durations.write_text(json.dumps({"s": known | {"test_top.py": 1.0}}))
    whole = _collect(root)
    assert len(whole) == 8 * 5 + 1
    return ShardSuite(root, imported, durations, whole)


@pytest.mark.parametrize("balanced", [False, True], ids=["hash", "balanced-with-unknown-files"])
@pytest.mark.parametrize("count", [2, 3, 4])
def test_file_shards_equal_unsharded_collection_without_importing_other_shards(
    shard_suite: ShardSuite, count: int, balanced: bool
) -> None:
    options: list[str] = []
    if balanced:
        # Only some files are in the table; d..h are new since it was made.
        options = ["--ci-shard-suite=s", f"--ci-shard-durations={shard_suite.durations}"]

    union: list[str] = []
    for index in range(count):
        for mark in shard_suite.imported.iterdir():
            mark.unlink()
        shard = _collect(
            shard_suite.root,
            "--ci-shard-by=file",
            *options,
            f"--ci-shard-index={index}",
            f"--ci-shard-count={count}",
        )
        files = {nodeid.partition("::")[0] for nodeid in shard}
        assert files.isdisjoint(nodeid.partition("::")[0] for nodeid in union)
        union += shard
        # A directory walk skips other shards' files before importing them.
        assert {m.name for m in shard_suite.imported.iterdir()} == {
            Path(f).stem.removeprefix("test_") for f in files if f.startswith("suite/")
        }

    assert Counter(union) == Counter(shard_suite.whole)


def test_files_are_placed_greedily_longest_first(tmp_path: Path) -> None:
    table = tmp_path / "durations.json"
    weights = {"a.py": 10.0, "b.py": 9.0, "c.py": 5.0, "d.py": 5.0, "e.py": 1.0}
    table.write_text(json.dumps({"s": weights}))

    placed = {path: file_shard(path, 2, "s", table) for path in weights}

    # a -> 0 (10); b -> 1 (9); c -> 1 (14); d -> 0 (15); e -> 1 (15).
    assert placed == {"a.py": 0, "b.py": 1, "c.py": 1, "d.py": 0, "e.py": 1}
    assert projected_loads(2, "s", table) == [15.0, 15.0]


def test_equal_weights_break_ties_by_path_then_shard_index(tmp_path: Path) -> None:
    table = tmp_path / "durations.json"
    table.write_text(json.dumps({"s": {"z.py": 4.0, "a.py": 4.0, "m.py": 4.0}}))

    assert [file_shard(p, 2, "s", table) for p in ("a.py", "m.py", "z.py")] == [0, 1, 0]


@pytest.mark.parametrize("count", [1, 2, 3, 4])
def test_unknown_files_land_in_exactly_one_shard(tmp_path: Path, count: int) -> None:
    table = tmp_path / "durations.json"
    table.write_text(json.dumps({"s": {"known.py": 3.0}}))
    unknown = [f"pkg/tests/test_new_{i}.py" for i in range(50)]

    for suite in ("s", "other", None):
        for path in unknown:
            owners = [
                index for index in range(count) if file_shard(path, count, suite, table) == index
            ]
            assert len(owners) == 1
            assert file_shard(path, count, suite, table) == shard_for(path, count)


def test_missing_table_falls_back_to_hashing_by_file(tmp_path: Path) -> None:
    assert file_shard("a/test_x.py", 4, "core", tmp_path / "absent.json") == shard_for(
        "a/test_x.py", 4
    )
