"""Tests for daimon.adapters.slack.routines_panel.actions.

Covers handle_routine_action (pause / non-admin / cross-tenant). All cases
use a real Postgres schema (db_session_factory) + the transport-level
FakeSlackWebClient from conftest (no AsyncMock on client.* methods).
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest
import yarl
from cryptography.fernet import Fernet
from daimon.adapters.slack.routines_panel.actions import handle_routine_action
from daimon.adapters.slack.runtime import SlackRuntime
from daimon.core._models import Tenant
from daimon.core.github_credentials import build_multifernet, encrypt_token
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.stores.routines import create_routine, get_routine
from daimon.core.stores.slack_bot_tokens import upsert_slack_bot_token
from daimon.testing.ma import build_fake_anthropic, make_fake_ma_handler
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_TEAM_ID = "T_ACTIONS"
_USER_ID = "U_CREATOR"
_OTHER_USER_ID = "U_NON_ADMIN"
_VIEW_ID = "V_TEST"


async def _seed_team(
    session: AsyncSession,
    *,
    team_id: str = _TEAM_ID,
) -> tuple[uuid.UUID, str, bytes]:
    """Create tenant + bot token for a team. Returns (tenant_id, fernet_key, encrypted)."""
    fernet_key = Fernet.generate_key().decode()
    fernet = build_multifernet((fernet_key,))
    encrypted = encrypt_token(fernet, "xoxb-test")

    tenant_id = derive_tenant_uuid(platform="slack", workspace_id=team_id)
    session.add(Tenant(id=tenant_id, platform="slack", external_id=team_id))
    await session.flush()
    await upsert_slack_bot_token(session, team_id=team_id, encrypted_token=encrypted)
    await session.flush()
    return tenant_id, fernet_key, encrypted


def _build_runtime(fernet_key: str, db_factory: async_sessionmaker[AsyncSession]) -> SlackRuntime:
    settings = MagicMock()
    settings.crypto.keys = (SecretStr(fernet_key),)
    # Prod default: no dev admin override, so the delete gate exercises the real
    # admin-OR-creator path (MagicMock would otherwise make this truthy).
    settings.slack.dev_allow_all_admin = False
    return SlackRuntime(
        settings=settings,
        anthropic=build_fake_anthropic(make_fake_ma_handler()),
        sessionmaker=db_factory,
        http_client=MagicMock(spec=httpx.AsyncClient),
    )


def _pause_payload(
    routine_id: uuid.UUID, *, team_id: str = _TEAM_ID, user_id: str = _USER_ID
) -> dict[str, object]:
    return {
        "team": {"id": team_id},
        "user": {"id": user_id},
        "view": {"id": _VIEW_ID, "private_metadata": "C_TEST"},
        "actions": [
            {
                "action_id": f"routine_action:{routine_id}",
                "selected_option": {"value": "pause"},
            }
        ],
    }


def _delete_payload(
    routine_id: uuid.UUID, *, team_id: str = _TEAM_ID, user_id: str = _USER_ID
) -> dict[str, object]:
    return {
        "team": {"id": team_id},
        "user": {"id": user_id},
        "trigger_id": "TRIGGER_TEST",
        "view": {"id": _VIEW_ID, "private_metadata": "C_TEST"},
        "actions": [
            {
                "action_id": f"routine_action:{routine_id}",
                "selected_option": {"value": "delete"},
            }
        ],
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_routine_action_pause_flips_enabled_to_false_and_triggers_views_update(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: object,
) -> None:
    """Pause overflow flips enabled=False in the DB and calls views.update."""
    # Seed team + routine (creator = _USER_ID)
    tenant_id, fernet_key, _ = await _seed_team(db_session)
    routine = await create_routine(
        db_session,
        tenant_id=tenant_id,
        created_by_user_id=_USER_ID,
        agent_id="agent-x",
        agent_name="Test Agent",
        cron_expr="0 * * * *",
        timezone_="UTC",
        trigger_message="daily stand-up",
        enabled=True,
    )
    await db_session.flush()

    runtime = _build_runtime(fernet_key, db_session_factory)
    payload = _pause_payload(routine.id)

    await handle_routine_action(runtime, payload)  # type: ignore[arg-type]

    # Assert routine is now disabled
    async with db_session_factory() as s:
        updated = await get_routine(s, routine.id, tenant_id=tenant_id)
    assert updated is not None, "routine must still exist after pause"
    assert updated.enabled is False, "pause action should set enabled=False"

    # Assert views.update was called (transport-level check via mock.requests).
    # Use Any for fake_slack_web_client — importing FakeSlackWebClient resolves to
    # the wrong conftest (core) per the known pytest path resolution issue (82-01 dev).
    client_fake: Any = fake_slack_web_client
    views_update_calls = client_fake.mock.requests.get(
        ("POST", yarl.URL("https://slack.com/api/views.update"))
    )
    assert views_update_calls, "views.update must be called after a successful pause"


@pytest.mark.asyncio
async def test_handle_routine_action_non_admin_non_creator_leaves_enabled_unchanged(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: object,
) -> None:
    """A non-admin, non-creator click must NOT change the routine's enabled state."""
    tenant_id, fernet_key, _ = await _seed_team(db_session, team_id="T_GATE")
    routine = await create_routine(
        db_session,
        tenant_id=tenant_id,
        created_by_user_id="U_ORIGINAL_CREATOR",  # not the clicker
        agent_id="agent-y",
        agent_name="Test Agent",
        cron_expr="0 * * * *",
        timezone_="UTC",
        trigger_message="gate test routine",
        enabled=True,
    )
    await db_session.flush()

    runtime = _build_runtime(fernet_key, db_session_factory)
    # Clicker is _OTHER_USER_ID (non-creator); aioresponses default returns non-admin
    payload = _pause_payload(routine.id, team_id="T_GATE", user_id=_OTHER_USER_ID)

    await handle_routine_action(runtime, payload)  # type: ignore[arg-type]

    async with db_session_factory() as s:
        after = await get_routine(s, routine.id, tenant_id=tenant_id)
    assert after is not None, "routine must still exist"
    assert after.enabled is True, "non-admin/non-creator click must not change enabled state"


@pytest.mark.asyncio
async def test_handle_routine_action_cross_tenant_routine_id_is_refused_with_no_write(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: object,
) -> None:
    """Routine from tenant A sent via team B's payload must be refused without a write."""
    # Tenant A + routine
    tenant_a_id, _, _ = await _seed_team(db_session, team_id="T_CROSS_A")
    routine_a = await create_routine(
        db_session,
        tenant_id=tenant_a_id,
        created_by_user_id=_USER_ID,
        agent_id="agent-a",
        agent_name="Agent A",
        cron_expr="0 * * * *",
        timezone_="UTC",
        trigger_message="tenant A routine",
        enabled=True,
    )
    # Tenant B + its own token (so resolve_web_client succeeds for team B)
    _, fernet_key_b, _ = await _seed_team(db_session, team_id="T_CROSS_B")
    await db_session.flush()

    runtime = _build_runtime(fernet_key_b, db_session_factory)
    # Payload is from team B but references routine from tenant A
    payload = _pause_payload(routine_a.id, team_id="T_CROSS_B", user_id=_USER_ID)

    await handle_routine_action(runtime, payload)  # type: ignore[arg-type]

    async with db_session_factory() as s:
        after = await get_routine(s, routine_a.id, tenant_id=tenant_a_id)
    assert after is not None, "routine A must still exist"
    assert after.enabled is True, "cross-tenant routine_id must be refused without any DB write"


# ---------------------------------------------------------------------------
# Delete overflow → confirm modal
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_routine_action_delete_by_creator_pushes_confirm_modal_and_no_delete(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: object,
) -> None:
    """Delete overflow by the creator pushes a confirm modal and does NOT delete yet."""
    tenant_id, fernet_key, _ = await _seed_team(db_session, team_id="T_DEL_CREATOR")
    routine = await create_routine(
        db_session,
        tenant_id=tenant_id,
        created_by_user_id=_USER_ID,
        agent_id="agent-x",
        agent_name="Test Agent",
        cron_expr="0 * * * *",
        timezone_="UTC",
        trigger_message="deletable routine",
        enabled=True,
    )
    await db_session.flush()

    runtime = _build_runtime(fernet_key, db_session_factory)
    payload = _delete_payload(routine.id, team_id="T_DEL_CREATOR", user_id=_USER_ID)

    await handle_routine_action(runtime, payload)  # type: ignore[arg-type]

    # Routine must NOT be deleted yet — the confirm modal is only pushed.
    async with db_session_factory() as s:
        still = await get_routine(s, routine.id, tenant_id=tenant_id)
    assert still is not None, "delete overflow must not delete before confirmation"

    client_fake: Any = fake_slack_web_client
    views_push_calls = client_fake.mock.requests.get(
        ("POST", yarl.URL("https://slack.com/api/views.push"))
    )
    assert views_push_calls, "delete overflow by creator must push a confirm modal"


@pytest.mark.asyncio
async def test_handle_routine_action_delete_by_non_admin_non_creator_is_refused(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: object,
) -> None:
    """Delete overflow by a non-admin non-creator must not push a confirm modal."""
    tenant_id, fernet_key, _ = await _seed_team(db_session, team_id="T_DEL_GATE")
    routine = await create_routine(
        db_session,
        tenant_id=tenant_id,
        created_by_user_id="U_ORIGINAL_CREATOR",
        agent_id="agent-y",
        agent_name="Test Agent",
        cron_expr="0 * * * *",
        timezone_="UTC",
        trigger_message="gated routine",
        enabled=True,
    )
    await db_session.flush()

    runtime = _build_runtime(fernet_key, db_session_factory)
    payload = _delete_payload(routine.id, team_id="T_DEL_GATE", user_id=_OTHER_USER_ID)

    await handle_routine_action(runtime, payload)  # type: ignore[arg-type]

    async with db_session_factory() as s:
        still = await get_routine(s, routine.id, tenant_id=tenant_id)
    assert still is not None, "routine must still exist"

    client_fake: Any = fake_slack_web_client
    views_push_calls = client_fake.mock.requests.get(
        ("POST", yarl.URL("https://slack.com/api/views.push"))
    )
    assert not views_push_calls, "non-admin non-creator delete must not push a confirm modal"
