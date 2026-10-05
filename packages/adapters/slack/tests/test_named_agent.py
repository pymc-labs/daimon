"""The Slack mention path passes only the explicit name form to admission."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

from daimon.adapters.slack.app import SlackApp
from daimon.core.turn.errors import NamedAgentRefused
from slack_sdk.web.async_client import AsyncWebClient


async def test_slack_passes_named_mention_to_admit() -> None:
    app = object.__new__(SlackApp)
    app.runtime = MagicMock()
    app._bot_user_ids = {"T": "B"}
    web_client = MagicMock(spec=AsyncWebClient)
    web_client.chat_postMessage = AsyncMock()
    with (
        patch("daimon.adapters.slack.app.resolve_admin_status", new_callable=AsyncMock) as admin,
        patch("daimon.adapters.slack.app.admit", new_callable=AsyncMock) as admit,
    ):
        admin.return_value = True
        admit.side_effect = NamedAgentRefused("stop")
        await app._run_thread_turn_observed(
            {"user": "U", "text": "<@B> Planner: draft", "ts": "1"},
            channel="C",
            web_client=web_client,
            tenant_id=uuid.uuid4(),
            thread_id="1",
            team_id="T",
        )
    assert admit.await_args.kwargs["requested_agent_name"] == "Planner"
