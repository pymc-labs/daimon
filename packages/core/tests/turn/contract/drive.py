"""Run one host turn through the shared driver and a backend's real turn IO."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Literal

import pytest
from daimon.core.turn import driver
from daimon.core.turn.io import TurnIO
from daimon.core.turn.posture import BillingExempt
from daimon.core.turn.state import TurnState
from daimon.testing.turn_fakes import RecordingLifecycle
from mux.contracts.ids import ResourceRef, Scope
from mux.testing.drivers.anthropic.ma_transport import ScriptedTransport

from .wire import Progress

SCOPE = Scope(
    tenant_id="tenant", account_id="account", principal_id="host", authorization_id="contract"
)

READ_TIMEOUT_S = 0.2
"""The stream read timeout every case runs with, so a quiet stream turns over fast."""


def session(provider: str, account_scope_id: str = "project") -> ResourceRef:
    return ResourceRef(
        id="session",
        kind="session",
        provider=provider,
        account_scope_id=account_scope_id,
        tenant_id=SCOPE.tenant_id,
        account_id=SCOPE.account_id,
    )


@dataclass
class Result:
    state: TurnState
    lifecycle: RecordingLifecycle
    sends: int = 0
    """User-message deliveries the service accepted."""
    facts: dict[str, object] = field(default_factory=dict)

    def text(self) -> str:
        return "".join(block.text for block in self.state.content if block.kind == "text")


class _Lifecycle(RecordingLifecycle):
    """Records when the turn is finalized, next to the wire's interrupts."""

    def __init__(self, progress: Progress) -> None:
        super().__init__()
        self.progress = progress

    async def on_terminal_failure(self, state: TurnState, error: BaseException) -> None:
        self.progress.order.append("finalized")
        await super().on_terminal_failure(state, error)

    async def on_terminal_success(self, state: TurnState) -> None:
        self.progress.order.append("finalized")
        await super().on_terminal_success(state)


def _phased(
    monkeypatch: pytest.MonkeyPatch,
    io: TurnIO,
    progress: Progress,
    name: Literal["status", "replay"],
) -> None:
    """Mark the wire reads made inside `io.<name>`, so a script fails the read it
    names and no other. Only the marker is added; the IO's own method runs."""
    method = getattr(io, name)

    async def marked(*args: object, **kwargs: object) -> object:
        progress.phase = name
        try:
            return await method(*args, **kwargs)
        finally:
            progress.phase = None

    monkeypatch.setattr(io, name, marked)


async def drive(
    monkeypatch: pytest.MonkeyPatch,
    io: TurnIO,
    progress: Progress,
    *,
    deadline_s: float,
    session_id: str = "session",
) -> tuple[TurnState, RecordingLifecycle]:
    """The real `run_turn` on the mux path, with `io` as the backend's turn IO."""
    _phased(monkeypatch, io, progress, "status")
    _phased(monkeypatch, io, progress, "replay")
    monkeypatch.setattr(driver, "turn_io", lambda *args, **kwargs: io)
    transport = ScriptedTransport()
    lifecycle = _Lifecycle(progress)
    async with transport.client() as client:
        # The outer bound only catches a driver that ignores its own deadline.
        async with asyncio.timeout(deadline_s + 20):
            state = await driver.run_turn(
                anthropic=client,
                session_id=session_id,
                user_message="question",
                lifecycle=lifecycle,
                cancel=asyncio.Event(),
                billing=BillingExempt(reason="headless-unrecorded"),
                path="mux",
                scope=SCOPE,
                render_interval_s=3600,
                interrupt_timeout_s=1.0,
                stream_read_timeout_s=READ_TIMEOUT_S,
                deadline=datetime.now(UTC) + timedelta(seconds=deadline_s),
            )
    # The ancillary Managed Agents client is never the backend's wire.
    assert not transport.requests
    return state, lifecycle
