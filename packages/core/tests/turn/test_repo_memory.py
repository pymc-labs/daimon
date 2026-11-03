"""The common turn path chooses the memory principal and injection boundary."""

import uuid
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from daimon.core.config import RepoMemorySettings
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.repo_memory import MemoryState
from daimon.core.turn import run
from daimon.core.turn.admission import Admission
from daimon.core.turn.deps import TurnDeps
from daimon.core.turn.prepare import PreparedTurn


@pytest.mark.parametrize(
    ("case", "allowed", "personal"),
    [
        ("ordinary", True, True),
        ("native", True, True),
        ("shared_owner", True, False),
        ("unattended", True, False),
        ("hold", False, True),
        ("shared_channel", False, True),
        ("source_shared", False, True),
        ("unresolved", False, True),
        ("routine", True, False),
        ("disabled", False, False),
    ],
)
async def test_turn_memory_eligibility_and_principal(
    monkeypatch: pytest.MonkeyPatch,
    case: str,
    allowed: bool,
    personal: bool,
) -> None:
    tenant_id, account_id, mapping_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    admission = cast(
        Admission,
        SimpleNamespace(
            agent=SimpleNamespace(id="agent"),
            account_id=account_id,
            shared_owner=uuid.uuid4() if case == "shared_owner" else None,
            hold=object() if case == "hold" else None,
            shared_channel=case == "shared_channel",
            source_shared=case == "source_shared",
        ),
    )
    prepared = cast(
        PreparedTurn,
        SimpleNamespace(
            admission=admission,
            mapping_id=None if case == "native" else mapping_id,
            ma_session_id="session",
            session_ref=SimpleNamespace(provider="openai") if case == "native" else None,
        ),
    )
    deps = cast(
        TurnDeps,
        SimpleNamespace(
            repo_memory=RepoMemorySettings(enabled=case != "disabled"),
            sessionmaker=MagicMock(),
        ),
    )
    prefix = AsyncMock(return_value=MemoryState("memory", True))
    monkeypatch.setattr(run, "turn_memory_state", prefix)
    token = run.current_origin_id.set(None if case in {"unresolved", "routine"} else uuid.uuid4())
    try:
        result = await run._repo_memory_state(  # pyright: ignore[reportPrivateUsage]
            deps,
            prepared,
            tenant_id=tenant_id,
            origin="routine" if case == "routine" else "chat",
            unattended=case in {"unattended", "routine"},
        )
    finally:
        run.current_origin_id.reset(token)
    if case == "disabled":
        assert result.prefix == ""
        prefix.assert_not_called()
        return
    prefix.assert_awaited_once_with(
        deps.sessionmaker,
        tenant_id=tenant_id,
        agent_id=derive_agent_uuid(tenant_id=tenant_id, ma_agent_id="agent"),
        session_provider="openai" if case == "native" else "anthropic",
        session_id="session",
        account_id=account_id if personal else None,
        allowed=allowed,
    )
