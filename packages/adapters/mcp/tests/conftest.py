"""Shared fixtures for MCP adapter tests.

MA transport fakes (MARouter, build_fake_anthropic, list_response, sse_response,
send_events_response, json_body) are imported from daimon.testing.ma.

DB fixtures come from daimon.testing.db: one schema per worker, wiped before
every test by `db_clean`. `committing_sessionmaker` adds a factory on the
engine itself (a separate pool connection from `db_session`) for tests that
must observe real COMMIT visibility.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterator

import pytest
import pytest_asyncio
from daimon.testing.db import (  # noqa: F401
    db_clean,
    db_engine,
    db_nullpool_engine,
    db_schema,
    db_session,
    db_session_factory,
)
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
)


@pytest_asyncio.fixture(autouse=True)
async def isolate_confirmation_workers() -> AsyncIterator[None]:
    """An agent-chat send starts its turn's confirmation worker; one test's
    worker must not outlive it into the next test's fakes."""
    from daimon.adapters.mcp.tools import _agent_chat_safety

    yield
    workers = [w.task for w in _agent_chat_safety._workers.values()]  # pyright: ignore[reportPrivateUsage]
    _agent_chat_safety._workers.clear()  # pyright: ignore[reportPrivateUsage]
    for worker in workers:
        worker.cancel()
    await asyncio.gather(*workers, return_exceptions=True)


@pytest.fixture(autouse=True)
def isolate_agent_faces() -> Iterator[None]:
    """Creating an agent starts its face render in the background; one test's
    render must not write into the next test's database or back-off state."""
    from daimon.core.agent_identity import cancel_pending_agent_faces

    yield
    cancel_pending_agent_faces()


@pytest.fixture(autouse=True)
def reset_environment_chip() -> Iterator[None]:
    """create_mcp_app sets the page shell's environment chip process-wide; an
    app built with a non-empty name must not leak the chip into later pages."""
    from daimon.adapters.mcp.web_shell import configure_environment_label

    yield
    configure_environment_label(name="", label="")


@pytest_asyncio.fixture
async def sessionmaker(
    db_session_factory: async_sessionmaker[AsyncSession],  # noqa: F811
) -> async_sessionmaker[AsyncSession]:
    """Readable alias for `db_session_factory` used throughout MCP tests."""
    return db_session_factory


@pytest_asyncio.fixture
async def committing_sessionmaker(
    db_engine: AsyncEngine,  # noqa: F811
    db_clean: None,  # noqa: F811  # orders this factory after the per-test wipe
) -> async_sessionmaker[AsyncSession]:
    """Sessionmaker on a SEPARATE connection from db_session.

    Use for McpRuntime in mutation tests: data written through this factory
    is only visible from db_session after a real COMMIT. Catches missing
    .begin() bugs that the shared-connection ``sessionmaker`` fixture masks.

    Every `db_engine` connection is pinned to the worker schema, so no
    schema mapping is needed here.
    """
    return async_sessionmaker(bind=db_engine, expire_on_commit=False)


@pytest.fixture(autouse=True)
def current_discord_membership(monkeypatch: pytest.MonkeyPatch) -> None:
    """Existing MCP fixtures simulate a current non-admin server member.

    Explicit resolver fakes override this default in role/admin/error tests.
    No runtime constructed by this suite can accidentally call Discord REST.
    """
    from daimon.adapters.mcp.runtime import McpRuntime
    from daimon.core.membership import CachedMembershipResolver, Membership

    async def fetch(tenant_id, guild_id, user_id):  # type: ignore[no-untyped-def]
        return Membership(is_member=True)

    def initialize(runtime: McpRuntime) -> None:
        if runtime.membership_resolver is None:
            object.__setattr__(runtime, "membership_resolver", CachedMembershipResolver(fetch))

    monkeypatch.setattr(McpRuntime, "__post_init__", initialize)


@pytest.fixture
def platform_admin_edge(monkeypatch: pytest.MonkeyPatch) -> None:
    """Platform facts for transport tests; token claims never serve as attestation."""
    from daimon.adapters.mcp.middleware import mcp_identity
    from daimon.testing.admin_facts import platform_authority_from_test_accounts

    monkeypatch.setattr(mcp_identity, "resolve_authority", platform_authority_from_test_accounts)


@pytest.fixture(autouse=True)
def empty_accounts_for_mock_database(monkeypatch: pytest.MonkeyPatch) -> None:
    """Schema-only unit harnesses have no accounts; real DB tests stay untouched."""
    from unittest.mock import Mock

    from daimon.adapters.mcp.middleware import mcp_identity

    real = mcp_identity.get_account_with_tenant

    async def lookup(session, *, account_id):
        if isinstance(session, Mock):
            return None
        return await real(session, account_id=account_id)

    monkeypatch.setattr(mcp_identity, "get_account_with_tenant", lookup)
