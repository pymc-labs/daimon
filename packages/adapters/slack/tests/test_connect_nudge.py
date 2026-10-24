"""A first mention gets no account-connect tip.

The connect link is offered when a read actually needs the person's Slack
account (`build_connect_hint` in the read and search tools), not before the
first answer.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, patch

from daimon.core.defaults.provisioning import provision_tenant
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.turn.state import TextBlock, TurnState
from mux.testing.drivers.anthropic.ma_models import ma_session, ma_session_agent
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .harness import make_orchestrate_app


async def test_first_mention_posts_no_connect_tip(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
) -> None:
    """The deployment can mint connect links and the person has never connected,
    yet answering their first mention posts nothing ephemeral."""
    team_id = "T_NO_TIP"
    channel = "C1"
    thread_ts = "9000000099.000001"
    tenant_id = derive_tenant_uuid(platform="slack", workspace_id=team_id)

    await provision_tenant(
        db_session_factory, platform="slack", workspace_id=team_id, signup_credit=Decimal("10")
    )

    app, _ = make_orchestrate_app(db_session_factory, app_root_url="https://daimon.example.com")

    async def _fake_run_turn(*, lifecycle: Any, **kwargs: Any) -> TurnState:
        state = TurnState(content=[TextBlock(kind="text", text="Hello!")])
        await lifecycle.on_terminal_success(state)
        return state

    event: dict[str, Any] = {
        "type": "app_mention",
        "ts": thread_ts,
        "event_ts": thread_ts,
        "channel": channel,
        "user": "U_NO_TIP",
        "text": "<@U_BOT> hello",
    }

    _fake_session = ma_session(
        id="sess-no-tip-001",
        agent=ma_session_agent(id="agent_no_tip_id"),
        environment_id="env_no_tip_id",
    )

    with (
        patch(
            "daimon.core.turn.admission.resolve_agent", new_callable=AsyncMock
        ) as mock_resolve_agent,
        patch(
            "daimon.core.turn.admission.resolve_environment", new_callable=AsyncMock
        ) as mock_resolve_env,
        patch(
            "daimon.core.turn.prepare.create_session", new_callable=AsyncMock
        ) as mock_create_session,
        patch("daimon.core.turn.run.run_turn", new_callable=AsyncMock) as mock_run_turn,
    ):
        mock_resolve_agent.return_value = "agent_no_tip_id"
        mock_resolve_env.return_value = "env_no_tip_id"
        mock_create_session.return_value = _fake_session
        mock_run_turn.side_effect = _fake_run_turn

        await app._orchestrate(  # pyright: ignore[reportPrivateUsage]
            event,
            team_id=team_id,
            channel=channel,
            event_ts=thread_ts,
            web_client=fake_slack_web_client.client,
            tenant_id=tenant_id,
        )

    assert mock_run_turn.called, "the turn itself runs"
    ephemeral_calls = [
        req
        for (method, url), reqs in fake_slack_web_client.mock.requests.items()
        if method == "POST" and url.path == "/api/chat.postEphemeral"
        for req in reqs
    ]
    assert ephemeral_calls == [], "no account-connect tip before the first answer"
