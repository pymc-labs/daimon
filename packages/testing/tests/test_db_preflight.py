"""Fixture admission precedes engine allocation, schema DDL and cleanup."""

from __future__ import annotations

import importlib.metadata
import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest
from daimon.testing.db import (
    DatabasePreflight,
    isolated_test_schema,
    preflight_test_database,
)
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlalchemy.pool import NullPool

pytest_plugins = ["pytester"]

_LANE = "postgresql+asyncpg://localhost/daimon_test_nc_N8"
_SHARED = "postgresql+asyncpg://localhost/daimon_test"


@pytest.fixture
def clean_ci(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.delenv("CI", raising=False)


@pytest.mark.usefixtures("clean_ci")
@pytest.mark.parametrize("host", ["localhost", "127.0.0.1"])
@pytest.mark.parametrize("lane", ["N8", "n5_worker", "x" * 48])
def test_preflight_admits_lane_without_constructing_engine(
    monkeypatch: pytest.MonkeyPatch, host: str, lane: str
) -> None:
    import daimon.testing.db as db

    def forbidden(*args: object, **kwargs: object) -> None:
        raise RuntimeError("preflight constructed an engine")

    monkeypatch.setattr(db, "create_async_engine", forbidden)
    url = f"postgresql+asyncpg://fixture:private-marker@{host}/daimon_test_nc_{lane}"
    monkeypatch.setenv("DAIMON_DATABASE__TEST_URL", url)
    admitted = preflight_test_database(f"test_w{os.getpid()}_1234abcd")
    if admitted.url != url or "private-marker" in repr(admitted):
        raise RuntimeError("preflight changed configuration or exposed credentials")


@pytest.mark.usefixtures("clean_ci")
@pytest.mark.parametrize(
    "url",
    [
        "postgresql+asyncpg://fixture:private-marker@localhost/operator_test",
        "postgresql+asyncpg://localhost/test_production",
        "postgresql+asyncpg://localhost/daimon",
        "postgresql+asyncpg://remote.invalid/daimon_test_nc_N8",
        _LANE + "?host=remote.invalid",
        _SHARED + "?application_name=other",
        "postgresql+asyncpg://localhost/daimon_test_nc_",
        "postgresql+asyncpg://localhost/daimon_test_nc_N8-other",
        "postgresql+asyncpg://localhost/daimon_test_nc_" + "x" * 49,
        "sqlite:///daimon_test_nc_N8",
        "mysql://localhost/daimon_test_nc_N8",
        "private-marker-not-a-url",
        "postgresql+asyncpg://fixture:private-marker@localhost:bad-port/daimon_test_nc_N8",
    ],
)
def test_preflight_refuses_foreign_target_without_credentials_in_error(
    monkeypatch: pytest.MonkeyPatch, url: str
) -> None:
    monkeypatch.setenv("DAIMON_DATABASE__TEST_URL", url)
    with pytest.raises(ValueError, match="owned local") as caught:
        preflight_test_database(f"test_w{os.getpid()}_1234abcd")
    if "private-marker" in str(caught.value):
        raise RuntimeError("preflight refusal exposed credentials")


@pytest.mark.parametrize(
    "github,ci,kind,expected",
    [
        ("true", "true", "w", "admit"),
        ("true", "true", "f", "admit"),
        (None, None, "w", "skip"),
        (None, "true", "w", "skip"),
        ("true", None, "w", "skip"),
        ("TRUE", "true", "w", "skip"),
        ("true", "1", "w", "skip"),
        ("true", "true", "foreign", "refuse"),
        ("true", "true", "public", "refuse"),
        ("true", "true", "custom", "refuse"),
    ],
)
def test_ci_preflight_obeys_exact_flags_and_process_schema(
    monkeypatch: pytest.MonkeyPatch,
    github: str | None,
    ci: str | None,
    kind: str,
    expected: str,
) -> None:
    monkeypatch.setenv("DAIMON_DATABASE__TEST_URL", _SHARED)
    for name, value in (("GITHUB_ACTIONS", github), ("CI", ci)):
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)
    schema = {
        "w": f"test_w{os.getpid()}_1234abcd",
        "f": f"test_f{os.getpid()}_1234abcd",
        "foreign": f"test_w{os.getpid() + 1}_1234abcd",
        "public": "public",
        "custom": "test_custom",
    }[kind]
    if expected == "skip":
        with pytest.raises(pytest.skip.Exception, match="owned lane"):
            preflight_test_database(schema)
    elif expected == "refuse":
        with pytest.raises(ValueError, match="owned local"):
            preflight_test_database(schema)
    elif preflight_test_database(schema) != DatabasePreflight(_SHARED, schema):
        raise RuntimeError("CI preflight changed the fixture configuration")


def test_missing_dsn_fails_without_allocating_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DAIMON_DATABASE__TEST_URL", raising=False)
    with pytest.raises(RuntimeError, match="must be set"):
        preflight_test_database(f"test_w{os.getpid()}_1234abcd")


def test_plugin_is_installed_without_conftest_imports() -> None:
    points = importlib.metadata.entry_points(group="pytest11")
    if not any(
        point.name == "daimon_db_preflight" and point.value == "daimon.testing.pytest_plugin"
        for point in points
    ):
        raise RuntimeError("shared preflight plugin is not installed")


def test_database_free_tests_load_plugin_without_reading_dsn_or_models(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DAIMON_DATABASE__TEST_URL", raising=False)
    # Needs a fresh interpreter: it asserts which modules the plugin leaves
    # unimported, and an in-process run shares this process's sys.modules.
    pytester.makepyfile(
        """
import sys
def test_no_database(request):
    if not request.config.pluginmanager.has_plugin("daimon_db_preflight"):
        raise RuntimeError("preflight plugin missing")
    if "daimon.testing.db" in sys.modules:
        raise RuntimeError("plugin loaded database models for a database-free test")
"""
    )
    pytester.runpytest_subprocess("-q", timeout=120).assert_outcomes(passed=1)


async def test_valid_real_fixture_pools_and_schema_remain_unchanged(
    db_preflight: DatabasePreflight,
    db_engine: AsyncEngine,
    db_nullpool_engine: AsyncEngine,
    db_schema: str,
) -> None:
    if (
        db_preflight.schema != db_schema
        or isolated_test_schema(db_engine) != db_schema
        or isolated_test_schema(db_nullpool_engine) != db_schema
        or not isinstance(db_nullpool_engine.pool, NullPool)
    ):
        raise RuntimeError("preflight changed fixture schema or NullPool behavior")
    actual: list[tuple[str, str]] = []
    for engine in (db_engine, db_nullpool_engine):
        async with engine.connect() as connection:
            actual.append(
                (
                    await connection.scalar(text("SELECT current_database()")),
                    await connection.scalar(text("SELECT current_schema()")),
                )
            )
    if actual != [(db_engine.url.database, db_schema), (db_engine.url.database, db_schema)]:
        raise RuntimeError("fixture pools do not reach the admitted physical database/schema")


@pytest.mark.usefixtures("db_preflight")
def test_core_and_testing_conftest_reexports_keep_their_real_schema(
    pytester: pytest.Pytester,
) -> None:
    """Run the evaluator's exact cross-package combination in one worker."""
    root = Path(__file__).resolve().parents[3]
    result = pytester.runpytest_subprocess(
        "-q",
        "-n",
        "0",
        "-c",
        str(root / "pyproject.toml"),
        str(root / "packages/core/tests/test_agent_faces.py"),
        f"{Path(__file__).resolve()}::test_valid_real_fixture_pools_and_schema_remain_unchanged",
        timeout=180,
    )
    result.assert_outcomes(passed=12, deselected=1)


@pytest.mark.usefixtures("db_preflight")
def test_engine_fixture_aliases_share_worker_schema_lifetime(pytester: pytest.Pytester) -> None:
    """A local base-engine alias and the next module's engine share one owner."""
    pytester.makeconftest(
        """
import pytest
import daimon.testing.db as shared
from daimon.testing.db import db_engine, db_schema

@pytest.fixture(scope="session", autouse=True)
def schema_lifetime():
    counts = {"create": 0, "drop": 0}
    create = shared._create_schema_with_tables
    drop = shared._drop_schema
    patch = pytest.MonkeyPatch()
    async def tracked_create(*args):
        counts["create"] += 1
        await create(*args)
    async def tracked_drop(*args):
        counts["drop"] += 1
        await drop(*args)
    patch.setattr(shared, "_create_schema_with_tables", tracked_create)
    patch.setattr(shared, "_drop_schema", tracked_drop)
    yield
    patch.undo()
    assert counts == {"create": 1, "drop": 1}
"""
    )
    first = pytester.path / "test_alias.py"
    first.write_text(
        """
import daimon.testing.db as shared
from sqlalchemy import text
base_db_engine = shared.db_engine

async def test_alias(base_db_engine, db_engine, db_schema):
    assert base_db_engine is db_engine
    async with base_db_engine.begin() as connection:
        await connection.execute(text(f'CREATE TABLE "{db_schema}".fixture_lifetime (n int)'))
        await connection.execute(text(f'INSERT INTO "{db_schema}".fixture_lifetime VALUES (7)'))
"""
    )
    second = pytester.path / "test_next_module.py"
    second.write_text(
        """
from sqlalchemy import text
async def test_next_module(db_engine, db_schema):
    async with db_engine.connect() as connection:
        assert await connection.scalar(text(f'SELECT n FROM "{db_schema}".fixture_lifetime')) == 7
"""
    )
    root = Path(__file__).resolve().parents[3]
    result = pytester.runpytest_inprocess(
        "-q",
        "-n",
        "0",
        "-p",
        "no:randomly",
        "-c",
        str(root / "pyproject.toml"),
        str(first),
        str(second),
    )
    result.assert_outcomes(passed=2)


@pytest.mark.parametrize("ci", [False, True])
def test_distinct_schema_definitions_each_admit_before_session_consumers(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, ci: bool
) -> None:
    """Conftest and class overrides cannot reuse another schema's admission."""
    monkeypatch.setenv("DAIMON_DATABASE__TEST_URL", _SHARED if ci else _LANE)
    for name in ("CI", "GITHUB_ACTIONS"):
        if ci:
            monkeypatch.setenv(name, "true")
        else:
            monkeypatch.delenv(name, raising=False)
    pytester.makeconftest(
        """
import os
import pytest
@pytest.fixture(scope="session")
def db_schema():
    return f"test_w{os.getpid()}_1234abcd"
@pytest.fixture(scope="session")
def admitted_consumer(db_preflight, db_schema):
    if db_preflight.schema != db_schema:
        raise RuntimeError("session consumer received another schema's admission")
    return db_preflight
"""
    )
    first = pytester.path / "first"
    second = pytester.path / "second"
    first.mkdir()
    second.mkdir()
    (first / "test_schema.py").write_text(
        """
def test_first(admitted_consumer, db_schema):
    if admitted_consumer.schema != db_schema:
        raise RuntimeError("first admission changed")
"""
    )
    (second / "conftest.py").write_text(
        """
import os
import pytest
@pytest.fixture(scope="session")
def db_schema():
    return f"test_w{os.getpid()}_5678abcd"
"""
    )
    (second / "test_other_schema.py").write_text(
        """
import os
import pytest
def test_second(admitted_consumer, db_preflight, db_schema):
    if admitted_consumer.schema != db_schema or db_preflight.schema != db_schema:
        raise RuntimeError("second admission changed")
class TestClassOverride:
    @pytest.fixture(scope="session")
    def db_schema(self):
        return f"test_w{os.getpid()}_90abcdef"
    def test_class(self, admitted_consumer, db_preflight, db_schema):
        if admitted_consumer.schema != db_schema or db_preflight.schema != db_schema:
            raise RuntimeError("class admission changed")
"""
    )
    pytester.runpytest_inprocess("-q").assert_outcomes(passed=3)


def _import_probe_module(path: Path) -> None:
    # pydantic imports this lazily, and a sys.modules restore would unload it.
    importlib.import_module("pydantic.root_model")
    spec = importlib.util.spec_from_file_location("new15_owned_preload", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)


@pytest.mark.parametrize(
    "case,expected_builds,expected_connections,expected_skips",
    [
        ("foreign", 0, 0, 0),
        ("remote", 0, 0, 0),
        ("local_shared", 0, 0, 8),
        ("ci_foreign_schema", 0, 0, 0),
        ("lane", 1, 1, 0),
        ("ci", 1, 1, 0),
        ("ci_unregistered", 1, 0, 0),
        ("injected_remote", 1, 0, 0),
    ],
)
def test_real_fixture_graph_stops_before_connection_or_ddl(
    pytester: pytest.Pytester,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
    expected_builds: int,
    expected_connections: int,
    expected_skips: int,
) -> None:
    monkeypatch.setenv("PREFLIGHT_PROBE_CASE", case)
    probe_file = Path(__file__).with_name("test_channel_accounting.py")
    monkeypatch.setenv("PREFLIGHT_NEW15_TEST_FILE", str(probe_file))
    # The inner session runs in this process and pytester restores sys.modules
    # after each run. Import the probe's heavy provider stack first, so it is
    # part of the snapshot and survives instead of being half-unloaded (which
    # breaks pydantic generics on the next case).
    _import_probe_module(probe_file)
    # No db_preflight import: existing consumers discover it automatically.
    pytester.makeconftest(
        r"""
import json
import importlib.util
import os
import sys
from pathlib import Path
import pytest
import daimon.testing.db as shared
from daimon.testing.db import (
    db_schema, db_engine, db_nullpool_engine, db_clean, db_session, db_session_factory,
)
from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import create_async_engine

spec = importlib.util.spec_from_file_location("new15_owned_probe", os.environ["PREFLIGHT_NEW15_TEST_FILE"])
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
context = module.context

@pytest.fixture(scope="session", autouse=True)
def connection_and_factory_trap():
    patch = pytest.MonkeyPatch()
    case = os.environ["PREFLIGHT_PROBE_CASE"]
    url = "postgresql+asyncpg://localhost/daimon_test_nc_N8"
    if case in ("ci", "ci_foreign_schema", "ci_unregistered", "local_shared"):
        url = "postgresql+asyncpg://localhost/daimon_test"
    elif case == "foreign":
        url = "postgresql+asyncpg://localhost/operator_test"
    elif case == "remote":
        url = "postgresql+asyncpg://remote.invalid/daimon_test_nc_N8"
    patch.setenv("DAIMON_DATABASE__TEST_URL", url)
    for name in ("CI", "GITHUB_ACTIONS"):
        if case.startswith("ci"):
            patch.setenv(name, "true")
        else:
            patch.delenv(name, raising=False)
    if case == "ci_foreign_schema":
        patch.setattr(shared, "_new_schema_name", lambda _: f"test_w{os.getpid()+1}_1234abcd")
    counts = {"builds": 0, "connections": 0, "ddl": 0, "body": 0}
    original_builder = shared.build_test_engine
    def allocate(*args, **kwargs):
        counts["builds"] += 1
        if case == "ci_unregistered":
            return create_async_engine(url)
        if case == "injected_remote":
            return original_builder("postgresql+asyncpg://remote.invalid/daimon_test_nc_N8", args[1])
        return original_builder(*args, **kwargs)
    def connection_trap(*args, **kwargs):
        counts["connections"] += 1
        raise RuntimeError("ACCEPTED_CONFIGURATION_CONNECTION_TRAP")
    async def ddl_trap(*args, **kwargs):
        counts["ddl"] += 1
        raise RuntimeError("UNEXPECTED_SCHEMA_DDL")
    patch.setattr(shared, "build_test_engine", allocate)
    patch.setattr(shared, "_create_schema_with_tables", ddl_trap)
    event.listen(Engine, "do_connect", connection_trap)
    yield counts
    event.remove(Engine, "do_connect", connection_trap)
    patch.undo()
    Path("trap.json").write_text(json.dumps(counts))
"""
    )
    pytester.makeini(
        """[pytest]
asyncio_mode = auto
asyncio_default_fixture_loop_scope = session
asyncio_default_test_loop_scope = session
"""
    )
    pytester.makepyfile(
        """
import pytest
@pytest.mark.parametrize("fixture", ["db_engine", "db_nullpool_engine", "db_clean", "db_session", "db_session_factory"])
def test_old_conftest_consumers(request, fixture, connection_and_factory_trap):
    request.getfixturevalue(fixture)
    connection_and_factory_trap["body"] += 1
    raise RuntimeError("test body must never run past trap")

async def test_original_new15_consumer(context, connection_and_factory_trap):
    connection_and_factory_trap["body"] += 1
    raise RuntimeError("NEW15 context must never run past trap")
"""
    )
    # The inner session runs in this process, but its conftest traps every
    # engine connection and all schema DDL, so no real DB access can escape.
    result = pytester.runpytest_inprocess("-q", "--tb=short")
    if expected_skips:
        result.assert_outcomes(skipped=expected_skips)
    else:
        result.assert_outcomes(failed=5, errors=3)
        diagnostic = (
            "ACCEPTED_CONFIGURATION_CONNECTION_TRAP" if expected_connections else "owned local"
        )
        result.stdout.fnmatch_lines([f"*{diagnostic}*"])
    actual = json.loads((pytester.path / "trap.json").read_text())
    if actual != {
        "builds": expected_builds,
        "connections": expected_connections,
        "ddl": 0,
        "body": 0,
    }:
        raise RuntimeError(f"fixture preflight/connection ordering changed: {actual}")
