"""A sealed provider turn's children carry its seals: they ride the credential, not metadata."""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.tools import compute
from daimon.adapters.mcp.tools.agent_chat import _bound_posture
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.permissions import session_readable_from
from daimon.core.session_seal import child_seal_ids, seal_ids, session_facts
from daimon.core.stores.domain import Role

from ..harness import auth_identity

_SEALED = "sealed-room"


def _provider_auth(seals: frozenset[str]) -> AuthIdentity:
    return auth_identity(
        uuid.uuid4(),
        uuid.uuid4(),
        role=Role.USER,
        platform="discord",
        chat_session_id="chat",
        seal_ids=seals,
    )


def _readable_from(metadata: dict[str, str], inside: set[str]) -> bool:
    facts = session_facts(metadata, owned=True)
    return session_readable_from(
        TenantAccessPolicy(),
        inside,
        channel=facts.channel,
        thread=facts.thread,
        seal_ids=facts.seal_ids,
        legacy_thread_id=None,
    )


@pytest.mark.parametrize(
    ("parent", "turn", "expected"),
    [
        ({"daimon_sealed": "a,b"}, (), {"a", "b"}),
        ({}, (_SEALED,), {_SEALED}),
        ({"daimon_sealed": "a"}, ("b",), {"a", "b"}),
        (None, (), set()),
    ],
    ids=["ma-metadata-only", "provider-claim-only", "union", "unsealed"],
)
def test_child_seal_is_the_parents_and_the_turns(parent, turn, expected) -> None:
    assert child_seal_ids(parent, turn) == expected, "metadata and credential seals both apply"


@pytest.mark.parametrize("sealed", [True, False], ids=["provider-sealed", "ma-unchanged"])
async def test_compute_child_stamp_is_sealed_by_the_provider_credential(
    monkeypatch, sealed
) -> None:
    """The parent has no seal metadata; the stamped source seals the child to the turn's seals."""

    @asynccontextmanager
    async def db():
        yield None

    auth = _provider_auth(frozenset({_SEALED}) if sealed else frozenset())
    origin = SimpleNamespace(
        platform="discord",
        thread_id="thread",
        responder_ma_agent_id="agent",
        parent_channel_id="parent",
    )
    monkeypatch.setattr(compute._session_gate, "session_card_gap", AsyncMock(return_value=None))
    monkeypatch.setattr(
        compute,
        "get_live_thread_session",
        AsyncMock(return_value=SimpleNamespace(ma_session_id="parent-session")),
    )
    monkeypatch.setattr(
        compute,
        "retrieve_session",
        AsyncMock(
            return_value=SimpleNamespace(
                id="parent-session", agent=SimpleNamespace(id="agent"), metadata={}
            )
        ),
    )
    events = [
        SimpleNamespace(
            type="agent.mcp_tool_use",
            name="compute_launch_session",
            mcp_server_name=compute.DAIMON_SERVER_NAME,
            input={"template": "mmm", "origin_context_id": "origin", "region": "us-central1"},
            id="tool-call",
        ),
        SimpleNamespace(type="user.tool_confirmation", result="allow", tool_use_id="tool-call"),
    ]
    monkeypatch.setattr(
        compute, "turn_io", lambda *a, **k: SimpleNamespace(replay=AsyncMock(return_value=events))
    )

    _, metadata = await compute._approved(
        SimpleNamespace(session_factory=db, client=None),
        auth,
        origin,
        "compute_launch_session",
        {
            "template": "mmm",
            "origin_context_id": "origin",
            "region": "us-central1",
            "ttl_hours": None,
        },
        {"ttl_hours": None},
    )

    if sealed:
        assert seal_ids(metadata) == {_SEALED}, "the child is sealed to the turn's seals"
        assert not _readable_from(metadata, {"parent", "thread"}), "unreadable outside the seal"
        assert _readable_from(metadata, {"parent", "thread", _SEALED}), "readable inside it"
    else:
        assert "daimon_sealed" not in metadata, "an unsealed Managed Agents turn stamps no seal"


async def test_agent_chat_session_carries_the_provider_turns_seals_read_only() -> None:
    """Even an unbound caller's agent-chat session is sealed and keeps memory read-only."""
    runtime = SimpleNamespace()
    seal, memory_read_only = await _bound_posture(
        runtime, _provider_auth(frozenset({_SEALED})), agent_names=()
    )
    assert seal == {_SEALED}, "the credential's seals stamp the child session"
    assert memory_read_only, "a sealed child cannot write shared memory"

    seal, memory_read_only = await _bound_posture(
        runtime, _provider_auth(frozenset()), agent_names=()
    )
    assert (seal, memory_read_only) == (frozenset(), False), "no seal, no change"
