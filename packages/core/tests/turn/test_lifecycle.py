"""Protocol-shape tests for TurnLifecycle."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import cast

import pytest
from anthropic.types import RawMessageStreamEvent
from daimon.core.turn.lifecycle import TurnLifecycle
from daimon.core.turn.state import TurnState


class _MinimalImpl(TurnLifecycle):
    """Implements only the three mandatory hooks. Should still be a TurnLifecycle
    because the new hooks (on_sse_event, on_reconnect, on_rate_limited,
    on_interrupt_sent) have default no-op bodies on the Protocol.

    Subclasses TurnLifecycle explicitly so the default async no-op bodies are
    inherited at runtime — pure structural typing would not give us those
    bodies (Python only hands down Protocol method bodies through MRO, not
    duck typing).
    """

    async def on_render(self, state: TurnState) -> None:  # noqa: ARG002
        return None

    async def on_terminal_success(self, state: TurnState) -> None:  # noqa: ARG002
        return None

    async def on_terminal_failure(self, state: TurnState, err: Exception) -> None:  # noqa: ARG002
        return None


def test_minimal_impl_satisfies_protocol_via_subclassing() -> None:
    impl: TurnLifecycle = _MinimalImpl()
    assert impl is not None, "the three-hook impl still satisfies the widened protocol"


@pytest.mark.asyncio
async def test_default_no_op_hooks_are_callable_without_override() -> None:
    impl: TurnLifecycle = _MinimalImpl()
    # These must be callable on any TurnLifecycle even when the impl did not
    # override them — default Protocol bodies supply the no-op.
    await impl.on_sse_event(cast(RawMessageStreamEvent, object()))
    await impl.on_reconnect("connection_dropped")
    await impl.on_rate_limited(datetime.now(UTC))
    await impl.on_rate_limited(None)
    await impl.on_interrupt_sent("sigint")
