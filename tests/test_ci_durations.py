"""scripts/generate_ci_durations.py and the duration-table guard in scripts/check_ci_shards.py."""

import importlib.util
import json
from pathlib import Path
from types import ModuleType

import pytest

_ROOT = Path(__file__).resolve().parents[1]


def _load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, _ROOT / "scripts" / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


generate = _load("generate_ci_durations")
check = _load("check_ci_shards")

_SUITES = {"core": 6, "discord": 2, "discord-mux": 2, "cli-run-mux": 1, "slack": 1}


@pytest.mark.parametrize(
    ("stem", "suite"),
    [
        ("core-shard-2", "core"),
        ("discord-shard-1", "discord"),
        ("discord-mux-shard-0", "discord-mux"),
        ("cli-run-mux-shard-0", "cli-run-mux"),
        ("slack", "slack"),
        ("notebook-host-browser", None),
        ("discordian-shard-0", None),
    ],
)
def test_a_junit_file_belongs_to_the_longest_suite_its_name_starts_with(
    stem: str, suite: str | None
) -> None:
    assert generate.suite_of(stem, _SUITES) == suite


def test_the_suite_names_cover_every_split_suite_of_the_workflow() -> None:
    shards = check.sharded_suites(_ROOT / ".github/workflows/ci.yml")
    assert {suite for suite, count in shards.items() if count > 1} >= {
        "core",
        "mcp",
        "discord",
        "discord-mux",
    }
    for suite in shards:
        assert generate.suite_of(f"{suite}-shard-0", shards) == suite


def _junit(path: Path, cases: dict[str, float]) -> Path:
    body = "".join(
        f'<testcase classname="{classname}" name="t" time="{seconds}"/>'
        for classname, seconds in cases.items()
    )
    path.write_text(f"<testsuites><testsuite>{body}</testsuite></testsuites>")
    return path


def test_build_keeps_discord_mux_apart_from_discord_and_skips_unsplit_suites(
    tmp_path: Path,
) -> None:
    (tmp_path / "pkg").mkdir()
    for name in ("test_a", "test_b"):
        (tmp_path / "pkg" / f"{name}.py").write_text("")
    files = [
        _junit(tmp_path / "discord-shard-0.xml", {"pkg.test_a": 1.0}),
        _junit(tmp_path / "discord-mux-shard-1.xml", {"pkg.test_b": 2.0}),
        _junit(tmp_path / "cli-run-mux-shard-0.xml", {"pkg.test_a": 3.0}),
    ]

    table, unresolved = generate.build(files, tmp_path, None, _SUITES)

    assert table == {"discord": {"pkg/test_a.py": 1.0}, "discord-mux": {"pkg/test_b.py": 2.0}}
    assert unresolved == 0


def test_the_guard_names_split_suites_missing_from_the_table() -> None:
    table = {"core": {"a.py": 1.0}, "discord": {}}

    problems = check.unlisted_suites(_SUITES, table)

    assert [problem.split(":")[0] for problem in problems] == ["discord", "discord-mux"]


def test_the_committed_table_lists_every_split_suite() -> None:
    table = json.loads(check.DURATIONS.read_text())
    shards = check.sharded_suites(_ROOT / ".github/workflows/ci.yml")

    assert check.unlisted_suites(shards, table) == []
