"""Shared memory is read-only for a DM turn when the server's audience mode is `on`.

The existing `dm_memory_read_only` switch and a narrow room's seal (via admission) already
made it read-only; the mode adds to them. `shadow` logs and changes nothing; `off` is today.
"""

import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
import structlog.testing
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools import agent_memory as tools
from daimon.core import host_memory
from daimon.core.host_memory import HostMemoryPlace, choose_store
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.provider_routine import routine_scope_thread
from daimon.core.stores.domain import Role, TurnOriginRow
from daimon.core.turn_hold import TurnHold
from fastmcp.exceptions import ToolError

from ..harness import auth_identity

DM = "dm:fictional-1"
ROOM = "room-thread"


def _origin(
    tenant: uuid.UUID, account: uuid.UUID, thread: str, *, read_only: bool
) -> TurnOriginRow:
    return TurnOriginRow(
        id=uuid.uuid4(),
        tenant_id=tenant,
        account_id=account,
        chat_session_id="signed-session",
        memory_read_only=read_only,
        human_invocation=True,
        attended=True,
        platform="discord",
        parent_channel_id="channel",
        thread_id=thread,
        responder_ma_agent_id="ma_agent",
        responder_name="Agent",
        configuration_target_ma_agent_id=None,
        configuration_target_name=None,
        role=Role.USER,
        created_at=datetime.now(UTC),
        expires_at=datetime.now(UTC),
    )


class _Session:
    async def __aenter__(self) -> object:
        return object()

    async def __aexit__(self, *_: object) -> None:
        return None


async def _open(
    monkeypatch: pytest.MonkeyPatch,
    *,
    mode: str,
    thread: str,
    read_only: bool = False,
    switch: bool = False,
    personal: bool = False,
) -> HostMemoryPlace:
    tenant, account = uuid.uuid4(), uuid.uuid4()
    auth: AuthIdentity = auth_identity(
        account,
        tenant,
        role=Role.USER,
        platform="discord",
        chat_agent_id=derive_agent_uuid(tenant_id=tenant, ma_agent_id="ma_agent"),
        no_confirmation_surface=True,
    )
    runtime = cast(
        "McpRuntime",
        SimpleNamespace(client=object(), settings=object(), session_factory=_Session),
    )

    async def read_mode(*_: object, **__: object) -> str:
        return mode

    on: Callable[[str], bool] = lambda key: key != "dm_memory_read_only" or switch  # noqa: E731
    monkeypatch.setattr(host_memory, "audience_mode", read_mode)
    monkeypatch.setattr(tools, "anthropic_configured", lambda _client: True)
    monkeypatch.setattr(
        tools,
        "require_turn_origin",
        AsyncMock(return_value=_origin(tenant, account, thread, read_only=read_only)),
    )
    monkeypatch.setattr(tools, "resolve_features", AsyncMock(return_value=SimpleNamespace(on=on)))
    memory = await tools.open_host_memory(
        runtime, auth, origin_context_id="origin", personal=personal
    )
    return cast("Any", memory)._place  # pyright: ignore[reportPrivateUsage]


@pytest.mark.parametrize(
    ("mode", "thread", "read_only", "switch", "expected"),
    [
        ("on", DM, False, False, True),
        ("on", ROOM, False, False, False),
        ("shadow", DM, False, False, False),
        ("off", DM, False, False, False),
        ("off", DM, False, True, True),
        ("off", ROOM, True, False, True),
        ("on", ROOM, True, False, True),
    ],
    ids=["on-dm", "on-room", "shadow-dm", "off-dm", "switch-dm", "narrow-room", "on-narrow-room"],
)
async def test_a_dm_turns_shared_memory_follows_the_mode(
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    thread: str,
    read_only: bool,
    switch: bool,
    expected: bool,
) -> None:
    place = await _open(monkeypatch, mode=mode, thread=thread, read_only=read_only, switch=switch)
    assert place.read_only is expected


async def test_shadow_logs_the_turn_on_would_change_and_never_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with structlog.testing.capture_logs() as logs:
        await _open(monkeypatch, mode="shadow", thread=DM)
        await _open(monkeypatch, mode="shadow", thread=DM, switch=True)
        await _open(monkeypatch, mode="shadow", thread=ROOM)
    events = [entry for entry in logs if entry["event"] == "audience.shadow"]
    assert len(events) == 1, "only the turn that `on` would change"
    assert events[0]["kind"] == "memory_read_only" and events[0]["agent"] == "Agent"
    assert {"tenant_id", "channel_id", "reason"} <= events[0].keys()


async def test_mode_off_logs_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    with structlog.testing.capture_logs() as logs:
        await _open(monkeypatch, mode="off", thread=DM)
    assert not [entry for entry in logs if entry["event"] == "audience.shadow"]


def test_a_contained_room_uses_the_channel_store_only() -> None:
    """A held turn reaches its channel's store and no shared or personal one."""
    place = HostMemoryPlace(
        tenant_id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        agent_name="Agent",
        account_id=uuid.uuid4(),
        hold=TurnHold("discord", "channel", True),
        shared_thread=True,
        read_only=False,
    )
    kind = choose_store(place, personal=False, shared_on=True, personal_on=True, channel_on=True)
    assert kind == "channel"
    with pytest.raises(host_memory.HostMemoryError):
        choose_store(place, personal=True, shared_on=True, personal_on=True, channel_on=True)


@pytest.mark.parametrize(
    ("channel_id", "allowed"), [("channel", False), (None, True)], ids=["posts", "owner-only"]
)
async def test_a_provider_routine_fire_reads_personal_memory_only_where_claude_mounts_it(
    monkeypatch: pytest.MonkeyPatch, channel_id: str | None, allowed: bool
) -> None:
    """A fire whose result others see gets no personal store, as `headless_runner` mounts none."""
    routine_id = uuid.uuid4()
    row = SimpleNamespace(destination_kind=None, destination_id=None, channel_id=channel_id)
    monkeypatch.setattr(tools, "get_routine", AsyncMock(return_value=row), raising=False)
    thread = routine_scope_thread(routine_id)
    if allowed:
        place = await _open(monkeypatch, mode="off", thread=thread, personal=True)
        assert not place.routine_posts, "an owner-only routine keeps its personal memory"
        return
    with pytest.raises(ToolError, match="no personal memory"):
        await _open(monkeypatch, mode="off", thread=thread, personal=True)
