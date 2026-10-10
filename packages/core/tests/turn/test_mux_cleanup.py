"""Cleanup uses the authorized session without changing legacy archive calls."""

from types import SimpleNamespace
from typing import Literal, cast
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import pytest
from daimon.core import direct_messages
from daimon.core.errors import DaimonError
from daimon.core.stores.direct_messages import DirectMessageRow
from daimon.core.turn.deps import TurnDeps
from daimon.core.turn.io import MuxTurnIO
from daimon.core.turn.run import _archive_orphaned_session  # pyright: ignore[reportPrivateUsage]
from daimon.testing.ma import session_response
from daimon.testing.ma_transport import ScriptedReply, ScriptedTransport
from mux.contracts.ids import ResourceRef, Scope
from mux.contracts.ports import ManagedAgents
from mux.profiles.anthropic import MANAGED_AGENTS

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


@pytest.mark.parametrize("path", ["legacy", "mux"])
async def test_sealed_dm_retirement_uses_conversation_owner_scope_only_on_mux(
    monkeypatch: pytest.MonkeyPatch, path: Literal["legacy", "mux"]
) -> None:
    maker = MagicMock()
    quarantine = AsyncMock(return_value=[SESSION.id])
    monkeypatch.setattr(direct_messages, "quarantine_conversation", quarantine)
    transport = ScriptedTransport()
    if path == "legacy":
        transport.queue(
            ScriptedReply(
                "POST", "/v1/sessions/session/archive", session_response(session_id=SESSION.id)
            )
        )
    archive = AsyncMock(return_value=object())
    backend = cast(
        ManagedAgents,
        SimpleNamespace(
            sessions=SimpleNamespace(archive=archive), capabilities=lambda: MANAGED_AGENTS
        ),
    )
    resolver = MagicMock(return_value=SESSION)
    async with transport.client() as client:
        deps = cast(
            TurnDeps,
            SimpleNamespace(
                sessionmaker=maker,
                anthropic=client,
                turn_path=path,
                backend=backend,
                backend_session_ref=resolver,
            ),
        )
        conversation = cast(DirectMessageRow, SimpleNamespace(tenant_id=TENANT, account_id=ACCOUNT))
        with pytest.raises(DaimonError, match="This DM was started"):
            await direct_messages._quarantine(deps, conversation)  # pyright: ignore[reportPrivateUsage]
    transport.assert_consumed()
    quarantine.assert_awaited_once()
    if path == "mux":
        resolver.assert_called_once_with(SESSION.id, SCOPE)
        archive.assert_awaited_once()
        assert archive.await_args is not None
        assert archive.await_args.args == (SCOPE, SESSION)
        assert transport.requests == []
    else:
        resolver.assert_not_called()
        archive.assert_not_called()
        assert len(transport.requests) == 1
        request = transport.requests[0]
        assert request.method == "POST" and request.path == "/v1/sessions/session/archive"
        original = ScriptedTransport()
        original.queue(
            ScriptedReply(
                "POST", "/v1/sessions/session/archive", session_response(session_id=SESSION.id)
            )
        )
        async with original.client() as unchanged_client:
            await unchanged_client.beta.sessions.archive(SESSION.id)
        original.assert_consumed()
        assert request.to_dict() == original.requests[0].to_dict()
