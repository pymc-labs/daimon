"""The catalog admin callback routes each frozen admin source to its real binding."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from daimon.testing.admin_bindings import AdminBindings, AdminReceipt
from daimon.testing.catalog_admin import MCP_TOKEN_CALL, CatalogAdmin
from daimon.testing.db import DatabasePreflight, truncate_all
from daimon.testing.headless_bindings import (
    QA_TOKEN_MCP_URL,
    DbCheckInvalid,
    db_check,
    loopback_transport,
)
from daimon.testing.qa_mcp_host import SharedQaHost, build_run, end_run, serve
from mux.contracts.ids import Provider
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

# Verbatim from the frozen catalog.
NEW27_STEP: dict[str, Any] = {
    "do": "admin",
    "tool": "headless",
    "args": {
        "call": "credential_submit.mcp_token",
        "token": "wrong-token-123",
        "url": "{env.qa_token_mcp_url}",
    },
}
NEW27_SQL = "select used_at is null from credential_requests where id = '{headless.request_id}'"
D7_STEP: dict[str, Any] = {
    "do": "admin",
    "tool": "cli",
    "args": "daimon channels skills add discord {guild_id} {channel_id} {env.qa_library_skill}",
}
ENV = {"env.qa_token_mcp_url": QA_TOKEN_MCP_URL}


@pytest.fixture
def db_session_factory(
    db_preflight: DatabasePreflight,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> async_sessionmaker[AsyncSession]:
    """The suite's factory, after the shared preflight has admitted this database.

    `db_preflight` resolves first: it skips the contributor's plain local
    `daimon_test` and refuses every other unowned target before any engine.
    """
    del db_preflight
    return db_session_factory


@pytest.fixture
async def mcp_transport(
    db_preflight: DatabasePreflight,
    qa_shared_host: SharedQaHost,
    tmp_path: Path,
) -> AsyncIterator[httpx.AsyncBaseTransport]:
    del db_preflight  # admitted first: the shared host is built on the same database
    qa = await build_run(qa_shared_host, port=0, root=tmp_path)
    try:
        async with serve(qa) as url:
            yield loopback_transport(url)
    finally:
        await end_run(qa)


async def test_new27_runs_through_the_admin_callback_for_every_backend(
    mcp_transport: httpx.AsyncBaseTransport,
    db_session_factory: async_sessionmaker[AsyncSession],
    db_engine: AsyncEngine,
    db_schema: str,
) -> None:
    # One QA host serves all three backends. Each run seeds the same fixed "fresh
    # install" tenant, so the worker schema is wiped between runs as db_clean does
    # between tests.
    backends: tuple[Provider, ...] = ("anthropic", "openai", "gemini")
    for backend in backends:
        async with db_engine.begin() as conn:
            await truncate_all(conn, db_schema)
        admin = CatalogAdmin(backend, db_session_factory, mcp_transport=mcp_transport)
        receipt = await admin(NEW27_STEP, ENV)

        assert receipt.backend == backend
        assert (receipt.status, receipt.operation, receipt.code) == (
            "executed",
            MCP_TOKEN_CALL,
            None,
        )
        assert receipt.observations["outcome"] == "token_rejected"
        assert receipt.observations["probe_statuses"] == [401]
        assert receipt.observations["used"] is False  # released for a retry since main's #611
        # The cell's own assertion then runs on the values the step produced.
        with pytest.raises(DbCheckInvalid, match="SQLSTATE 42703"):
            await db_check(db_session_factory, NEW27_SQL, receipt.values)


@pytest.mark.parametrize("backend", ["anthropic", "openai", "gemini"])
async def test_the_receipt_names_the_backend_it_was_built_for(
    backend: Provider, owned: _Owned
) -> None:
    """The label is all `backend` does, so the other backends need no host of their own."""

    def no_egress(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"egress attempted: {request.url}")

    unbound = await CatalogAdmin(backend, owned.factory)(NEW27_STEP, ENV)
    assert (unbound.status, unbound.backend, unbound.code) == (
        "pending",
        backend,
        "ADMIN_MCP_HOST_UNBOUND",
    )
    refused = await CatalogAdmin(
        backend, owned.factory, mcp_transport=httpx.MockTransport(no_egress)
    )({**NEW27_STEP, "allow_fail": True}, ENV)
    assert (refused.status, refused.backend) == ("pending", backend)


@pytest.mark.parametrize(
    ("step", "values", "code"),
    [
        ({**NEW27_STEP, "args": {**NEW27_STEP["args"], "call": "other"}}, ENV, "HEADLESS_CALL"),
        (NEW27_STEP, {}, "ADMIN_ARGUMENTS_UNBOUND"),
        ({**NEW27_STEP, "args": {**NEW27_STEP["args"], "extra": "x"}}, ENV, "ARGUMENTS"),
        ({**NEW27_STEP, "allow_fail": True}, ENV, "ADMIN_SOURCE_UNSUPPORTED"),
        (D7_STEP, ENV, "ADMIN_PRODUCT_BINDINGS_UNBOUND"),
    ],
)
async def test_an_unbindable_source_stays_typed_pending_with_no_effect(
    step: dict[str, Any], values: dict[str, str], code: str, owned: _Owned
) -> None:
    def no_egress(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"egress attempted: {request.url}")

    admin = CatalogAdmin("openai", owned.factory, mcp_transport=httpx.MockTransport(no_egress))
    receipt = await admin(step, values)
    assert receipt.status == "pending"
    assert receipt.code is not None and code in receipt.code


async def test_every_other_source_goes_to_the_product_admin_bindings(owned: _Owned) -> None:
    seen: list[tuple[Mapping[str, Any], Mapping[str, str]]] = []

    from daimon.testing.headless_bindings import isolated_anthropic

    class _Product:
        anthropic = isolated_anthropic(lambda request: httpx.Response(500))
        sessions = owned.factory

        async def execute(
            self, source: Mapping[str, Any], values: Mapping[str, str]
        ) -> AdminReceipt:
            seen.append((source, values))
            return AdminReceipt(status="pending", backend="openai", operation="channel_skills_add")

    admin = CatalogAdmin("openai", owned.factory, product=cast(AdminBindings, _Product()))
    receipt = await admin(D7_STEP, ENV)
    assert seen == [(D7_STEP, ENV)]
    assert receipt.operation == "channel_skills_add"


# Credential isolation: no ambient Anthropic override reaches any client.

AMBIENT = ("ANTHROPIC_WEBHOOK_SIGNING_KEY", "ANTHROPIC_CUSTOM_HEADERS", "ANTHROPIC_BASE_URL")


class _NeverOpened(async_sessionmaker[AsyncSession]):
    def __call__(self, **local_kw: object) -> AsyncSession:  # type: ignore[override]
        raise AssertionError("a refused or pending binding opened a session")


@dataclass
class _Owned:
    factory: async_sessionmaker[AsyncSession]
    connects: list[object]


@pytest.fixture
def owned(db_session_factory: async_sessionmaker[AsyncSession]) -> _Owned:
    """This run's own (lane or registered CI) bind, so the shared guard passes; never opened."""
    from sqlalchemy import event

    bind = db_session_factory.kw["bind"]
    connects: list[object] = []
    event.listen(bind.engine.sync_engine, "connect", lambda *_: connects.append(1))
    return _Owned(_NeverOpened(bind=bind), connects)


@pytest.mark.parametrize("name", AMBIENT)
@pytest.mark.parametrize("value", ["", "https://ambient.invalid\nX-Ambient: 1"])
async def test_an_ambient_anthropic_setting_is_refused_before_any_effect(
    name: str, value: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, owned: _Owned
) -> None:
    from daimon.testing.headless_bindings import (
        BindingRefused,
        serve_catalog_hosts,
        submit_mcp_token,
    )

    def no_egress(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"egress attempted: {request.url}")

    class _Product:
        sessions = owned.factory

        async def execute(self, *_: object) -> AdminReceipt:
            raise AssertionError("the product binding ran under an ambient override")

    monkeypatch.setenv(name, value)
    sessions = owned.factory
    transport = httpx.MockTransport(no_egress)
    with pytest.raises(BindingRefused, match=name):
        await submit_mcp_token(sessions, mcp_transport=transport, token="t")
    with pytest.raises(BindingRefused, match=name):
        async with serve_catalog_hosts(sessions, tmp_path):
            pass
    admin = CatalogAdmin(
        "openai", sessions, mcp_transport=transport, product=cast(AdminBindings, _Product())
    )
    for step in (NEW27_STEP, D7_STEP):
        with pytest.raises(BindingRefused, match=name):
            await admin(step, ENV)
    assert owned.connects == []


async def test_the_isolated_client_takes_nothing_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from daimon.testing.headless_bindings import isolated_anthropic
    from mux.testing.isolated_sdk import AmbientSDKConfigurationError

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"egress attempted: {request.url}")

    client = isolated_anthropic(handler)
    assert client.webhook_key is None
    assert client.base_url.host == "anthropic.invalid"
    assert client._custom_headers == {}  # pyright: ignore[reportPrivateUsage]
    # Set after construction: every copy path re-checks before the SDK re-reads it.
    for name in AMBIENT:
        monkeypatch.setenv(name, "late")
        with pytest.raises(AmbientSDKConfigurationError, match=name):
            client.copy()
        with pytest.raises(AmbientSDKConfigurationError, match=name):
            client.with_options(timeout=1)
        monkeypatch.delenv(name)
    assert client.with_options(timeout=1).webhook_key is None
    await client.close()


@pytest.mark.parametrize(
    "kwargs",
    [
        {"webhook_key": "retained-signing-key"},
        {"base_url": "https://api.anthropic.com"},
        {"default_headers": {"X-Retained": "1"}},
    ],
)
async def test_a_product_client_that_retained_ambient_settings_is_refused(
    kwargs: dict[str, Any], owned: _Owned
) -> None:
    from anthropic import AsyncAnthropic
    from daimon.testing.headless_bindings import BindingRefused

    class _Product:
        anthropic = AsyncAnthropic(
            api_key="x",
            base_url=kwargs.get("base_url", "https://anthropic.invalid"),
            webhook_key=kwargs.get("webhook_key", ""),
            default_headers=kwargs.get("default_headers"),
        )
        sessions = owned.factory

        async def execute(self, *_: object) -> AdminReceipt:
            raise AssertionError("ran with a client that retained ambient settings")

    admin = CatalogAdmin("openai", owned.factory, product=cast(AdminBindings, _Product()))
    with pytest.raises(BindingRefused, match="retained"):
        await admin(D7_STEP, ENV)


@pytest.mark.parametrize("where", ["callback", "product"])
async def test_a_remote_per_model_bind_is_refused_before_any_forwarding(
    where: str, owned: _Owned
) -> None:
    """One routed bind on either factory points elsewhere: refused, nothing connects."""
    from daimon.testing.headless_bindings import (
        BindingRefused,
        credential_request_route,
        isolated_anthropic,
    )
    from sqlalchemy import event
    from sqlalchemy.ext.asyncio import create_async_engine

    remote = create_async_engine(
        "postgresql+asyncpg://daimon:daimon@db.example.com:5432/daimon_test_nc_n2"
    )
    remote_connects: list[object] = []
    event.listen(remote.sync_engine, "connect", lambda *_: remote_connects.append(1))
    routed = _NeverOpened(bind=owned.factory.kw["bind"], binds=credential_request_route(remote))

    class _Product:
        anthropic = isolated_anthropic(lambda request: httpx.Response(500))
        sessions = routed if where == "product" else owned.factory

        async def execute(self, *_: object) -> AdminReceipt:
            raise AssertionError("forwarded with an unowned routed bind")

    admin = CatalogAdmin(
        "openai",
        routed if where == "callback" else owned.factory,
        product=cast(AdminBindings, _Product()),
    )
    for step in (NEW27_STEP, D7_STEP):
        with pytest.raises(BindingRefused):
            await admin(step, ENV)
    assert owned.connects == remote_connects == []
