"""The Teams name form requires a bot mention, including in personal chats."""

from __future__ import annotations

import dataclasses
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

from daimon.adapters.teams.app import TeamsApp
from daimon.core.stores.domain import Role
from daimon.core.turn.errors import NamedAgentRefused

from .conftest import make_inbound


async def test_teams_passes_name_only_after_bot_mention() -> None:
    app = object.__new__(TeamsApp)
    app.runtime = MagicMock()
    admit = AsyncMock(side_effect=NamedAgentRefused("stop"))
    with (
        patch.object(TeamsApp, "_role", return_value=Role.ADMIN),
        patch.object(TeamsApp, "_say", new_callable=AsyncMock),
        patch("daimon.adapters.teams.app.admit", admit),
    ):
        for mentioned in (False, True):
            inbound = dataclasses.replace(make_inbound("Planner: draft"), bot_mentioned=mentioned)
            await app._run_turn_observed(
                inbound,
                uuid.uuid4(),
                handoff=None,
                reraise=False,
                continuation=None,
            )
            assert admit.await_args.kwargs["requested_agent_name"] == (
                "Planner" if mentioned else None
            )
