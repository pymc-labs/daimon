"""Tests for run_routines_delete_submission (routines_panel/submit.py).

Real Postgres + transport-level Slack/MA fakes. Covers:
- creator submit → routine row deleted + panel refreshed (views.update).
- non-admin non-creator submit → row remains (fail-closed re-gate).
- cross-tenant routine_id → row remains, no delete.
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest
import yarl
from daimon.adapters.slack.routines_panel.submit import run_routines_delete_submission
from daimon.adapters.slack.runtime import SlackRuntime
from daimon.core._models import Tenant
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.stores.routines import create_routine, get_routine
from daimon.testing.ma import build_fake_anthropic, make_fake_ma_handler

_TEAM_ID = "T_DEL_SUB"
_USER_ID = "U_DEL_CREATOR"
_OTHER_USER_ID = "U_DEL_OTHER"
_CHANNEL_ID = "C_DEL"
_ROOT_VIEW_ID = "V_ROOT"


def _build_runtime(db_session_factory: Any) -> SlackRuntime:
    settings: MagicMock = MagicMock()
    # Prod default so the re-gate exercises the real admin-OR-creator path.
    settings.slack.dev_allow_all_admin = False
    return SlackRuntime(
        settings=settings,
        anthropic=build_fake_anthropic(make_fake_ma_handler()),
        sessionmaker=db_session_factory,
        http_client=MagicMock(spec=httpx.AsyncClient),
    )


async def _seed_tenant(db_session_factory: Any, *, team_id: str = _TEAM_ID) -> uuid.UUID:
    tenant_id = derive_tenant_uuid(platform="slack", workspace_id=team_id)
    async with db_session_factory() as session:
        session.add(Tenant(id=tenant_id, platform="slack", external_id=team_id))
        await session.commit()
    return tenant_id


@pytest.mark.asyncio
async def test_run_routines_delete_by_creator_removes_row_and_refreshes_panel(
    fake_slack_web_client: Any,
    db_session_factory: Any,
) -> None:
    """A creator's confirm submit deletes the row and refreshes the panel view."""
    tenant_id = await _seed_tenant(db_session_factory)
    async with db_session_factory() as session:
        routine = await create_routine(
            session,
            tenant_id=tenant_id,
            created_by_user_id=_USER_ID,
            agent_id="agent-x",
            agent_name="Test Agent",
            cron_expr="0 * * * *",
            timezone_="UTC",
            trigger_message="deletable routine",
            enabled=True,
        )
        await session.commit()

    runtime = _build_runtime(db_session_factory)

    await run_routines_delete_submission(
        runtime,
        fake_slack_web_client.client,
        team_id=_TEAM_ID,
        user_id=_USER_ID,
        channel_id=_CHANNEL_ID,
        routine_id=str(routine.id),
        root_view_id=_ROOT_VIEW_ID,
    )

    async with db_session_factory() as session:
        gone = await get_routine(session, routine.id, tenant_id=tenant_id)
    assert gone is None, "creator confirm submit must delete the routine row"

    views_update_calls = fake_slack_web_client.mock.requests.get(
        ("POST", yarl.URL("https://slack.com/api/views.update"))
    )
    assert views_update_calls, "delete must refresh the panel via views.update"


@pytest.mark.asyncio
async def test_run_routines_delete_by_non_admin_non_creator_leaves_row(
    fake_slack_web_client: Any,
    db_session_factory: Any,
) -> None:
    """A non-admin non-creator confirm submit must NOT delete the row (fail-closed)."""
    tenant_id = await _seed_tenant(db_session_factory, team_id="T_DEL_GATE_SUB")
    async with db_session_factory() as session:
        routine = await create_routine(
            session,
            tenant_id=tenant_id,
            created_by_user_id="U_ORIGINAL",
            agent_id="agent-y",
            agent_name="Test Agent",
            cron_expr="0 * * * *",
            timezone_="UTC",
            trigger_message="gated routine",
            enabled=True,
        )
        await session.commit()

    runtime = _build_runtime(db_session_factory)

    await run_routines_delete_submission(
        runtime,
        fake_slack_web_client.client,
        team_id="T_DEL_GATE_SUB",
        user_id=_OTHER_USER_ID,
        channel_id=_CHANNEL_ID,
        routine_id=str(routine.id),
        root_view_id=_ROOT_VIEW_ID,
    )

    async with db_session_factory() as session:
        still = await get_routine(session, routine.id, tenant_id=tenant_id)
    assert still is not None, "non-admin non-creator submit must not delete the routine"


@pytest.mark.asyncio
async def test_run_routines_delete_cross_tenant_routine_id_is_refused(
    fake_slack_web_client: Any,
    db_session_factory: Any,
) -> None:
    """A routine from tenant A submitted under team B must not be deleted."""
    tenant_a = await _seed_tenant(db_session_factory, team_id="T_DEL_A")
    await _seed_tenant(db_session_factory, team_id="T_DEL_B")
    async with db_session_factory() as session:
        routine_a = await create_routine(
            session,
            tenant_id=tenant_a,
            created_by_user_id=_USER_ID,
            agent_id="agent-a",
            agent_name="Agent A",
            cron_expr="0 * * * *",
            timezone_="UTC",
            trigger_message="tenant A routine",
            enabled=True,
        )
        await session.commit()

    runtime = _build_runtime(db_session_factory)

    await run_routines_delete_submission(
        runtime,
        fake_slack_web_client.client,
        team_id="T_DEL_B",
        user_id=_USER_ID,
        channel_id=_CHANNEL_ID,
        routine_id=str(routine_a.id),
        root_view_id=_ROOT_VIEW_ID,
    )

    async with db_session_factory() as session:
        still = await get_routine(session, routine_a.id, tenant_id=tenant_a)
    assert still is not None, "cross-tenant routine_id must be refused without a delete"
