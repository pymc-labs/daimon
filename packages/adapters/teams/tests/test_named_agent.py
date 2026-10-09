"""The Teams name form requires a bot mention, including in personal chats."""

from __future__ import annotations

import dataclasses
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

from daimon.adapters.teams.app import TeamsApp
from daimon.adapters.teams.card import named_agent_notice_card
from daimon.core.stores.domain import Role
from daimon.core.turn.errors import NamedAgentRefused

from .conftest import make_inbound


async def test_teams_passes_name_only_after_bot_mention() -> None:
    app = object.__new__(TeamsApp)
    app.runtime = MagicMock()
    app._sender = MagicMock()  # pyright: ignore[reportPrivateUsage]
    app._sender.send = AsyncMock()  # pyright: ignore[reportPrivateUsage]
    admit = AsyncMock(side_effect=NamedAgentRefused(kind="unavailable"))
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


def test_named_notice_card_keeps_title_and_detail_separate() -> None:
    err = NamedAgentRefused(kind="thread", current_name="Daimon", named_name="Planner")
    payload = named_agent_notice_card(err).model_dump(exclude_none=True)
    card = payload["attachments"][0]["content"]
    assert card["body"] == [
        {
            "type": "TextBlock",
            "text": "This thread is with Daimon.",
            "weight": "Bolder",
            "wrap": True,
        },
        {
            "type": "TextBlock",
            "text": "Start a new message in the channel to ask Planner.",
            "wrap": True,
        },
    ]
