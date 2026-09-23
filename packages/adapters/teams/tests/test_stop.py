"""The Teams Stop button cancels the running turn.

Pressing Stop makes the next streamed send come back 403, and the SDK then
marks the stream ``canceled``. That has to reach the core ``cancel`` event,
or the Managed Agents turn keeps running and billing to completion while the
user sees it stopped.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from daimon.adapters.teams.turn_lifecycle import TeamsTurnLifecycle
from daimon.core.defaults.provisioning import provision_tenant
from daimon.core.turn.state import TurnState
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .conftest import ENTRA_TENANT_ID, FakeStream, make_dispatch_target, patched_turn_pipeline


@pytest.mark.asyncio
async def test_stop_sets_the_turn_cancel_event(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await provision_tenant(db_session_factory, platform="teams", workspace_id=ENTRA_TENANT_ID)
    dispatcher, ctx, activity = make_dispatch_target(db_session_factory)
    marked_ids: list[uuid.UUID] = []
    observed: list[bool] = []

    async def _long_run_turn(*, lifecycle: Any, cancel: asyncio.Event, **kwargs: Any) -> TurnState:
        state = TurnState(content=[])
        await lifecycle.on_render(state)
        ctx.stream.canceled = True  # the user pressed Stop; the SDK saw the 403
        await lifecycle.on_render(state)  # the driver's next render tick
        try:
            await asyncio.wait_for(cancel.wait(), timeout=1)
            observed.append(True)
        except TimeoutError:
            observed.append(False)
        return state

    with (
        patched_turn_pipeline(marked_ids),
        patch("daimon.core.turn.run.run_turn", new_callable=AsyncMock) as mock_run_turn,
    ):
        mock_run_turn.side_effect = _long_run_turn
        await dispatcher.dispatch(ctx, activity)
        await dispatcher.drain(timeout=30)

    assert observed == [True], "Stop must set the cancel event the driver races against"


@pytest.mark.asyncio
async def test_recovery_lifecycle_forwards_stop_to_its_fresh_cancel() -> None:
    fresh_cancel = asyncio.Event()
    stream = FakeStream()
    lifecycle = TeamsTurnLifecycle(stream=stream, message_id="m-1", cancel=fresh_cancel)  # pyright: ignore[reportArgumentType]
    stream.canceled = True
    await lifecycle.on_render(TurnState(content=[]))
    assert fresh_cancel.is_set()
    assert stream.updates == [], "a stopped stream must not be written to again"
