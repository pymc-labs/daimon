"""End-to-end dispatch: a real SDK ingress POST runs a core turn.

The discriminating test for this contribution: a personal-chat
``MessageActivity`` POSTed through the REAL Microsoft SDK route — the only
seam faked is the outbound Bot Framework transport (there is no Teams
service to reach) — must spawn a background task that runs
``run_prepared_turn``, render one progress message, and land the terminal
answer on the SAME Teams message id.

On the vendor base this file cannot exist (no Teams package); at the
contribution head it must pass.
"""

from __future__ import annotations

import dataclasses
import json
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from daimon.adapters.teams.app import (
    AuthorizedTeamsActivity,
    DirectCoreTurnDispatcher,
)
from daimon.adapters.teams.http_service import create_teams_http_service
from daimon.adapters.teams.identity import VerifiedTeamsTurnResolver
from daimon.core.config import TeamsSettings
from daimon.core.defaults.provisioning import provision_tenant
from daimon.core.turn.run import run_prepared_turn as real_run_prepared_turn
from daimon.core.turn.state import TextBlock, TurnState
from daimon.testing import ma_session, ma_session_agent
from daimon.testing.asgi import asgi_lifespan
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .conftest import (
    BOT_CLIENT_ID,
    ENTRA_TENANT_ID,
    TeamsApiFake,
    build_teams_client,
    build_teams_runtime,
    make_message_activity,
)


def _settings() -> TeamsSettings:
    return TeamsSettings(
        client_id=BOT_CLIENT_ID,
        client_secret=SecretStr("test-secret"),
        tenant_id=ENTRA_TENANT_ID,
        port=3978,
    )


def _service(
    fake: TeamsApiFake,
    db_factory: async_sessionmaker[AsyncSession],
) -> tuple[Any, DirectCoreTurnDispatcher]:
    """A real ingress service whose resolver/dispatcher are the production classes."""
    runtime = build_teams_runtime(db_factory)
    resolver = VerifiedTeamsTurnResolver(sessionmaker=db_factory, entra_tenant_id=ENTRA_TENANT_ID)
    dispatcher = DirectCoreTurnDispatcher(turn_deps=runtime.turn_deps, sessionmaker=db_factory)
    runtime = dataclasses.replace(runtime, resolver=resolver, dispatcher=dispatcher)
    service = create_teams_http_service(
        settings=_settings(), runtime=runtime, client=build_teams_client(fake)
    )
    return service, dispatcher


@pytest.mark.asyncio
async def test_personal_chat_message_runs_a_turn_on_one_message(
    db_session_factory: async_sessionmaker[AsyncSession],
    teams_api_fake: TeamsApiFake,
    entra_env: None,
    stub_bot_token: None,
) -> None:
    await provision_tenant(db_session_factory, platform="teams", workspace_id=ENTRA_TENANT_ID)
    service, dispatcher = _service(teams_api_fake, db_session_factory)

    ran_turn: list[dict[str, Any]] = []

    async def _fake_run_turn(*, lifecycle: Any, **kwargs: Any) -> TurnState:
        ran_turn.append(kwargs)
        state = TurnState(content=[TextBlock(kind="text", text="Hello from Teams!")])
        await lifecycle.on_terminal_success(state)
        return state

    ran_prepared: list[Any] = []

    async def _run_prepared_spy(*args: Any, **kwargs: Any) -> Any:
        ran_prepared.append((args, kwargs))
        return await real_run_prepared_turn(*args, **kwargs)

    with (
        patch(
            "daimon.core.turn.admission.resolve_agent", new_callable=AsyncMock
        ) as mock_resolve_agent,
        patch(
            "daimon.core.turn.admission.resolve_environment", new_callable=AsyncMock
        ) as mock_resolve_env,
        patch(
            "daimon.core.turn.admission.is_over_balance", new_callable=AsyncMock
        ) as mock_over_balance,
        patch("daimon.core.turn.admission.is_over_cap", new_callable=AsyncMock) as mock_over_cap,
        patch(
            "daimon.core.turn.prepare.create_session", new_callable=AsyncMock
        ) as mock_create_session,
        patch("daimon.core.turn.run.run_turn", new_callable=AsyncMock) as mock_run_turn,
        patch(
            "daimon.adapters.teams.app.run_prepared_turn", new_callable=AsyncMock
        ) as mock_run_prepared,
    ):
        mock_resolve_agent.return_value = "agent_test_id"
        mock_resolve_env.return_value = "env_test_id"
        mock_over_balance.return_value = False
        mock_over_cap.return_value = False
        mock_create_session.return_value = ma_session(
            id="sess-teams-1",
            agent=ma_session_agent(id="agent_test_id"),
            environment_id="env_test_id",
        )
        mock_run_turn.side_effect = _fake_run_turn
        mock_run_prepared.side_effect = _run_prepared_spy

        async with asgi_lifespan(service.app):
            # The sweep and the turn both open sessions on the test's single
            # shared connection — serialize the sweep first (production runs
            # them on a real pool, so this ordering is a test-only concern).
            assert service.boot_sweep_task is not None
            await service.boot_sweep_task
            transport = httpx.ASGITransport(app=service.app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.post("/api/messages", json=make_message_activity())
            assert response.status_code in (200, 201, 202), response.text

            # The SDK handler returns as soon as dispatch spawns the task —
            # the turn itself is a background asyncio task, proven here.
            await dispatcher.drain(timeout=30)

    assert mock_run_prepared.called, (
        "dispatch must drive run_prepared_turn — the direct-core default seam"
    )
    assert ran_turn, "the background task must reach the turn body"

    # The Teams streaming protocol sends EVERY chunk as POST
    # /v3/conversations/{id}/activities — in-place updates of one Teams
    # message are distinguished by the activity ``id`` and ``streaminfo``
    # entity in the body, not by HTTP method or URL.
    posts = [r for r in teams_api_fake.activity_requests if r.method == "POST"]
    assert len(posts) >= 2, "progress and terminal sends must both reach Bot Framework"

    message_id = teams_api_fake.next_message_id
    first, *rest = posts
    assert not first.body.get("id"), "the first send creates the Teams message"
    assert all(r.body.get("id") == message_id for r in rest), (
        "every later streamed send must address the SAME Teams message id"
    )

    final = rest[-1]
    stream_entities = [e for e in final.body.get("entities", []) if e.get("type") == "streaminfo"]
    assert any(e.get("streamType") == "final" for e in stream_entities), (
        "the last send must close the stream on the same message"
    )
    assert "Hello from Teams!" in json.dumps(final.body), (
        "the terminal card carrying the answer must land on the progress message"
    )


@pytest.mark.asyncio
async def test_duplicate_activity_id_does_not_start_a_second_turn(
    db_session_factory: async_sessionmaker[AsyncSession],
    teams_api_fake: TeamsApiFake,
    entra_env: None,
    stub_bot_token: None,
) -> None:
    """Bot Framework retries carry the same activity id — the second is dropped."""
    await provision_tenant(db_session_factory, platform="teams", workspace_id=ENTRA_TENANT_ID)
    service, dispatcher = _service(teams_api_fake, db_session_factory)

    seen: list[AuthorizedTeamsActivity] = []

    async def _spy_run_turn(self: Any, ctx: Any, activity: Any) -> None:
        seen.append(activity)
        # Skip the real turn body — dedup is decided before it starts.
        return None

    with patch.object(DirectCoreTurnDispatcher, "_run_turn", _spy_run_turn):
        async with asgi_lifespan(service.app):
            assert service.boot_sweep_task is not None
            await service.boot_sweep_task
            transport = httpx.ASGITransport(app=service.app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                payload = make_message_activity(activity_id="dup-1")
                first = await client.post("/api/messages", json=payload)
                second = await client.post("/api/messages", json=payload)
            assert first.status_code in (200, 201, 202)
            assert second.status_code in (200, 201, 202)
            await dispatcher.drain(timeout=10)

    assert [a.activity_id for a in seen] == ["dup-1"]
