"""Slack's routine result poster (FEAT-085)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from daimon.adapters.slack import routine_delivery as poster_mod
from daimon.adapters.slack.routine_delivery import make_slack_routine_poster
from daimon.adapters.slack.runtime import SlackRuntime
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.stores.access_policy import set_access_policy
from daimon.core.stores.domain import RoutineRow
from daimon.core.stores.routines import create_routine
from daimon.testing.factories import make_tenant
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


async def _routine(db_session: AsyncSession, *, kind: str, destination_id: str) -> RoutineRow:
    tenant = await make_tenant(db_session, platform="slack", workspace_id="T_ROUTINES")
    row = await create_routine(
        db_session,
        tenant_id=tenant.id,
        created_by_user_id="U1",
        agent_id="ag",
        agent_name="daimon",
        cron_expr="0 9 * * 1",
        timezone_="UTC",
        trigger_message="go",
        destination_kind=kind,  # type: ignore[arg-type]
        destination_id=destination_id,
    )
    await db_session.commit()
    return row.model_copy(update={"last_result_tail": "Done <!channel> ping <@U7>."})


def _poster(
    sm: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> tuple[Any, MagicMock]:
    client = MagicMock()
    client.chat_postMessage = AsyncMock()

    async def fake_resolve(runtime: object, *, team_id: str) -> object:
        assert team_id == "T_ROUTINES", "the client is built for the routine's own workspace"
        return client

    monkeypatch.setattr(poster_mod, "resolve_web_client", fake_resolve)
    runtime = cast(SlackRuntime, SimpleNamespace(sessionmaker=sm))
    return make_slack_routine_poster(runtime), client


async def test_posts_into_a_thread_without_broadcasting(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = await _routine(db_session, kind="thread", destination_id="C1:1717.5")
    post, client = _poster(db_session_factory, monkeypatch)

    outcome = await post(row)

    assert outcome.status == "delivered"
    kwargs = client.chat_postMessage.await_args.kwargs
    assert (kwargs["channel"], kwargs["thread_ts"]) == ("C1", "1717.5")
    assert "<!channel>" not in kwargs["text"], "a routine never broadcasts"
    assert "<@U7>" in kwargs["text"], "mentions of people survive"


async def test_a_protected_channel_is_refused(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = await _routine(db_session, kind="channel", destination_id="C_ANN")
    await set_access_policy(
        db_session,
        tenant_id=row.tenant_id,
        policy=TenantAccessPolicy(protected_channel_ids=("C_ANN",)),
    )
    await db_session.commit()
    post, client = _poster(db_session_factory, monkeypatch)

    outcome = await post(row)

    assert (outcome.status, outcome.note) == ("skipped", "protected_channel")
    client.chat_postMessage.assert_not_awaited()


async def test_a_malformed_thread_destination_is_skipped(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = await _routine(db_session, kind="thread", destination_id="C1")
    post, client = _poster(db_session_factory, monkeypatch)

    outcome = await post(row)

    assert (outcome.status, outcome.note) == ("skipped", "destination_unavailable")
    client.chat_postMessage.assert_not_awaited()
