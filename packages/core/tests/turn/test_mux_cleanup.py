"""Cleanup uses the authorized session without changing legacy archive calls."""

from dataclasses import replace
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import pytest
from daimon.core import direct_messages
from daimon.core.errors import DaimonError
from daimon.core.stores.direct_messages import DirectMessageRow
from daimon.core.turn.io import MuxTurnIO
from daimon.core.turn.run import _archive_orphaned_session  # pyright: ignore[reportPrivateUsage]
from daimon.testing.turn_fakes import deps_for
from mux.contracts.ids import ResourceRef, Scope
from mux.contracts.ports import ManagedAgents
from mux.profiles.anthropic import MANAGED_AGENTS
from mux.testing.drivers.anthropic.ma_transport import ScriptedTransport

TENANT = UUID(int=7)
ACCOUNT = UUID(int=8)
SCOPE = Scope(
    tenant_id=str(TENANT),
    account_id=str(ACCOUNT),
    principal_id="daimon",
    authorization_id="sealed-dm-retirement",
)
SESSION = ResourceRef(
    id="session",
    kind="session",
    provider="anthropic",
    account_scope_id="workspace",
    tenant_id=SCOPE.tenant_id,
    account_id=SCOPE.account_id,
)


async def test_mux_orphan_cleanup_archives_through_the_bound_port() -> None:
    archive = AsyncMock(return_value=object())
    backend = cast(
        ManagedAgents,
        SimpleNamespace(
            sessions=SimpleNamespace(archive=archive), capabilities=lambda: MANAGED_AGENTS
        ),
    )
    transport = ScriptedTransport()
    async with transport.client() as client:
        await _archive_orphaned_session(
            client, session_id=SESSION.id, io=MuxTurnIO(backend, SCOPE, SESSION)
        )
    archive.assert_awaited_once()
    assert archive.await_args is not None
    assert archive.await_args.args == (SCOPE, SESSION)
    assert archive.await_args.kwargs["key"]
    assert transport.requests == []


async def test_sealed_dm_retirement_uses_conversation_owner_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    maker = MagicMock()
    quarantine = AsyncMock(return_value=[SESSION.id])
    monkeypatch.setattr(direct_messages, "quarantine_conversation", quarantine)
    transport = ScriptedTransport()
    archive = AsyncMock(return_value=object())
    backend = cast(
        ManagedAgents,
        SimpleNamespace(
            sessions=SimpleNamespace(archive=archive), capabilities=lambda: MANAGED_AGENTS
        ),
    )
    resolver = MagicMock(return_value=SESSION)
    async with transport.client() as client:
        deps = replace(deps_for(client, maker), backend=backend, backend_session_ref=resolver)
        conversation = cast(DirectMessageRow, SimpleNamespace(tenant_id=TENANT, account_id=ACCOUNT))
        with pytest.raises(DaimonError, match="This DM was closed because its channel"):
            await direct_messages._quarantine(deps, conversation)  # pyright: ignore[reportPrivateUsage]
    transport.assert_consumed()
    quarantine.assert_awaited_once()
    resolver.assert_called_once_with(SESSION.id, SCOPE)
    archive.assert_awaited_once()
    assert archive.await_args is not None
    assert archive.await_args.args == (SCOPE, SESSION)
    assert transport.requests == []
