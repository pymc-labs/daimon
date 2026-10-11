"""Pre-connection policy for every engine a test session can route to."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Callable
from typing import cast

import pytest
import pytest_asyncio
from daimon.testing.db import (
    build_test_engine,
    isolated_test_schema,
    require_owned_sessionmaker,
)
from sqlalchemy import event, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession, async_sessionmaker

EngineFactory = Callable[[str, str | None], AsyncEngine]
_LANE = "postgresql+asyncpg://localhost/daimon_test_nc_N5"
_SHARED = "postgresql+asyncpg://localhost/daimon_test"


@pytest_asyncio.fixture
async def engines(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[EngineFactory]:
    from sqlalchemy.ext.asyncio import create_async_engine

    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.delenv("CI", raising=False)
    allocated: list[AsyncEngine] = []
    attempts: list[object] = []

    def allocate(url: str, schema: str | None = None) -> AsyncEngine:
        engine = build_test_engine(url, schema) if schema is not None else create_async_engine(url)
        allocated.append(engine)

        def refuse_connection(*args: object, **kwargs: object) -> None:
            attempts.append(engine)
            raise AssertionError("the ownership guard must never connect")

        event.listen(engine.sync_engine, "do_connect", refuse_connection)
        return engine

    try:
        yield allocate
    finally:
        if attempts:
            raise AssertionError("ownership validation attempted a connection")
        for engine in allocated:
            await engine.dispose()


@pytest.mark.parametrize("lane", ["N5", "N2", "n4_worker", "N6_qa", "7", "x" * 48])
@pytest.mark.parametrize("factory", [False, True])
async def test_owned_lane_main_bind_is_accepted_without_connecting(
    engines: EngineFactory, lane: str, factory: bool
) -> None:
    engine = engines(f"postgresql+asyncpg://127.0.0.1/daimon_test_nc_{lane}", None)
    subject = async_sessionmaker(engine) if factory else engine
    require_owned_sessionmaker(subject)


@pytest.mark.parametrize(
    "url",
    [
        _SHARED,
        "postgresql+asyncpg://localhost/operator_test",
        "postgresql+asyncpg://localhost/test_production",
        "postgresql+asyncpg://localhost/daimon_test_nc_",
        "postgresql+asyncpg://localhost/daimon_test_nc__N5",
        "postgresql+asyncpg://localhost/daimon_test_nc_N6-qa",
        "postgresql+asyncpg://localhost/daimon_test_nc_N5%2Fother",
        "postgresql+asyncpg://localhost/daimon_test_nc_" + "x" * 49,
        "postgresql+asyncpg://remote.invalid/daimon_test_nc_N5",
        "postgresql+asyncpg:///daimon_test_nc_N5",
        "postgresql+asyncpg://[::1]/daimon_test_nc_N5",
        _LANE + "?host=remote.invalid",
        _LANE + "?database=production",
        _LANE + "?options=-csearch_path%3Dpublic",
    ],
)
@pytest.mark.parametrize("location", ["engine", "main", "routed"])
async def test_foreign_bind_refused_in_every_position(
    engines: EngineFactory, url: str, location: str
) -> None:
    foreign = engines(url, None)
    if location == "engine":
        subject = foreign
    elif location == "main":
        subject = async_sessionmaker(foreign, binds={AsyncSession: engines(_LANE, None)})
    else:
        owned = engines(_LANE, None)
        subject = async_sessionmaker(owned, binds={AsyncSession: foreign})
    with pytest.raises(ValueError, match="owned local"):
        require_owned_sessionmaker(subject)


@pytest.mark.parametrize("connection", [False, True])
async def test_every_routed_owned_engine_or_connection_is_checked(
    engines: EngineFactory, connection: bool
) -> None:
    first = engines(_LANE, None)
    second = engines("postgresql+asyncpg://localhost/daimon_test_nc_N2", None)
    main = AsyncConnection(first) if connection else first
    route = AsyncConnection(second) if connection else second
    factory = async_sessionmaker(main, binds={AsyncSession: route, object: first})
    require_owned_sessionmaker(factory)


async def test_last_foreign_route_cannot_hide_behind_two_owned_routes(
    engines: EngineFactory,
) -> None:
    owned = engines(_LANE, None)
    foreign = engines(_SHARED, None)
    factory = async_sessionmaker(owned, binds={AsyncSession: owned, object: owned, str: foreign})
    with pytest.raises(ValueError, match="owned local"):
        require_owned_sessionmaker(factory)


async def test_foreign_connection_route_and_main_are_refused(engines: EngineFactory) -> None:
    owned = engines(_LANE, None)
    foreign = AsyncConnection(engines(_SHARED, None))
    for factory in (
        async_sessionmaker(foreign),
        async_sessionmaker(owned, binds={object: foreign}),
    ):
        with pytest.raises(ValueError, match="owned local"):
            require_owned_sessionmaker(factory)


@pytest.mark.parametrize("routed", [None, {}])
async def test_absent_or_empty_routes_do_not_change_main_policy(
    engines: EngineFactory, routed: dict[object, object] | None
) -> None:
    factory = async_sessionmaker(engines(_LANE, None), binds=routed)
    require_owned_sessionmaker(factory)


@pytest.mark.parametrize(
    "location,invalid",
    [
        (location, invalid)
        for location in ("subject", "main", "routed", "routing_map")
        for invalid in (None, object(), "engine", list[object](), 1)
        if location != "routing_map" or invalid is not None
    ],
)
async def test_malformed_inputs_and_unbound_factories_are_refused(
    engines: EngineFactory, invalid: object, location: str
) -> None:
    factory = async_sessionmaker(engines(_LANE, None))
    kwargs = cast(dict[str, object], factory.kw)
    if location == "subject":
        subject = cast(async_sessionmaker[AsyncSession] | AsyncEngine, invalid)
    elif location == "main":
        kwargs["bind"] = invalid
        subject = factory
    elif location == "routed":
        kwargs["binds"] = {object: invalid}
        subject = factory
    else:
        kwargs["binds"] = invalid
        subject = factory
    with pytest.raises(ValueError, match="owned local"):
        require_owned_sessionmaker(subject)


async def test_unbound_main_cannot_be_replaced_by_owned_routes(engines: EngineFactory) -> None:
    with pytest.raises(ValueError, match="owned local"):
        require_owned_sessionmaker(async_sessionmaker(binds={object: engines(_LANE, None)}))


@pytest.mark.parametrize("location", ["main", "routed"])
async def test_synchronous_engine_binds_are_refused(engines: EngineFactory, location: str) -> None:
    owned = engines(_LANE, None)
    factory = async_sessionmaker(owned)
    kwargs = cast(dict[str, object], factory.kw)
    kwargs["bind" if location == "main" else "binds"] = (
        owned.sync_engine if location == "main" else {object: owned.sync_engine}
    )
    with pytest.raises(ValueError, match="owned local"):
        require_owned_sessionmaker(factory)


@pytest.mark.parametrize("backend", ["mysql", "sqlite"])
@pytest.mark.parametrize("location", ["main", "routed"])
async def test_non_postgresql_backend_is_refused(
    engines: EngineFactory, backend: str, location: str
) -> None:
    foreign = engines(_LANE, None)
    foreign.sync_engine.url = make_url(f"{backend}://localhost/daimon_test_nc_N5")
    factory = async_sessionmaker(
        foreign if location == "main" else engines(_LANE, None),
        binds={object: foreign} if location == "routed" else None,
    )
    with pytest.raises(ValueError, match="owned local"):
        require_owned_sessionmaker(factory)


@pytest.mark.parametrize(
    "github,ci,registered,schema_kind,accepted",
    [
        ("true", "true", True, "worker", True),
        ("true", "true", True, "fresh", True),
        (None, "true", True, "worker", False),
        ("true", None, True, "worker", False),
        ("false", "true", True, "worker", False),
        ("true", "false", True, "worker", False),
        ("TRUE", "true", True, "worker", False),
        ("true", "1", True, "worker", False),
        ("", "true", True, "worker", False),
        ("true", "true", False, "worker", False),
        ("true", "true", True, "other_pid", False),
        ("true", "true", True, "public", False),
        ("true", "true", True, "custom", False),
        ("true", "true", True, "short_hex", False),
    ],
)
@pytest.mark.parametrize("location", ["engine", "main", "routed"])
async def test_ci_exception_requires_flags_and_builder_provenance_for_every_bind(
    engines: EngineFactory,
    monkeypatch: pytest.MonkeyPatch,
    github: str | None,
    ci: str | None,
    registered: bool,
    schema_kind: str,
    accepted: bool,
    location: str,
) -> None:
    schemas = {
        "worker": f"test_w{os.getpid()}_1234abcd",
        "fresh": f"test_f{os.getpid()}_1234abcd",
        "other_pid": f"test_w{os.getpid() + 1}_1234abcd",
        "public": "public",
        "custom": "test_custom",
        "short_hex": f"test_w{os.getpid()}_1234abc",
    }
    schema = schemas[schema_kind]
    shared = engines(_SHARED, schema if registered else None)
    if not registered:
        shared = shared.execution_options(isolated_test_schema=schema)
        assert isolated_test_schema(shared) is None
    for name, value in (("GITHUB_ACTIONS", github), ("CI", ci)):
        if value is not None:
            monkeypatch.setenv(name, value)
    if location == "engine":
        subject = shared
    elif location == "main":
        subject = async_sessionmaker(shared, binds={object: engines(_LANE, None)})
    else:
        subject = async_sessionmaker(engines(_LANE, None), binds={object: shared})
    if accepted:
        require_owned_sessionmaker(subject)
        assert isolated_test_schema(shared) == schema
    else:
        with pytest.raises(ValueError, match="owned local"):
            require_owned_sessionmaker(subject)


async def test_ci_flags_never_admit_a_registered_remote_or_query_override(
    engines: EngineFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("CI", "true")
    schema = f"test_w{os.getpid()}_1234abcd"
    for url in (
        "postgresql+asyncpg://remote.invalid/daimon_test",
        _SHARED + "?host=remote.invalid",
        "postgresql+asyncpg://localhost/operator_test",
    ):
        with pytest.raises(ValueError, match="owned local"):
            require_owned_sessionmaker(engines(url, schema))


async def test_refusal_diagnostics_do_not_include_database_credentials(
    engines: EngineFactory,
) -> None:
    engine = engines("postgresql+asyncpg://invented:secret-sentinel@localhost/operator_test", None)
    with pytest.raises(ValueError) as caught:
        require_owned_sessionmaker(engine)
    assert str(caught.value) == (
        "Test hooks require owned local lane engines or registered isolated CI schemas"
    )
    assert "secret-sentinel" not in str(caught.value)


async def test_ci_builder_provenance_does_not_transfer_to_engine_clones(
    engines: EngineFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("CI", "true")
    registered = engines(_SHARED, f"test_w{os.getpid()}_1234abcd")
    require_owned_sessionmaker(registered)
    clone = registered.execution_options(isolated_test_schema=isolated_test_schema(registered))
    assert isolated_test_schema(clone) is None
    with pytest.raises(ValueError, match="owned local"):
        require_owned_sessionmaker(async_sessionmaker(registered, binds={object: clone}))


@pytest.mark.parametrize("ci_metadata", [False, True])
async def test_real_fixture_sessionmaker_keeps_its_registered_schema_and_binding(
    db_preflight,
    db_engine: AsyncEngine,
    db_schema: str,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    ci_metadata: bool,
) -> None:
    actual_database = db_engine.url.database
    original_kwargs = dict(db_session_factory.kw)
    assert actual_database is not None
    if actual_database == "daimon_test" and not (
        os.environ.get("GITHUB_ACTIONS") == "true" and os.environ.get("CI") == "true"
    ):
        pytest.skip("guard admits only lane databases or isolated CI schemas")
    elif actual_database != "daimon_test":
        assert actual_database.startswith("daimon_test_nc_")
    if ci_metadata:
        # Metadata exercises CI policy while the real pool retains its approved
        # test DSN. SELECT independently proves the unchanged physical database.
        monkeypatch.setenv("GITHUB_ACTIONS", "true")
        monkeypatch.setenv("CI", "true")
        monkeypatch.setattr(db_engine.sync_engine, "url", db_engine.url.set(database="daimon_test"))
    require_owned_sessionmaker(db_engine)
    require_owned_sessionmaker(db_session_factory)
    assert db_session_factory.kw == original_kwargs
    async with db_session_factory() as session:
        assert await session.scalar(text("SELECT current_database()")) == actual_database
        assert await session.scalar(text("SELECT current_schema()")) == db_schema
        assert isolated_test_schema(db_engine) == db_schema
