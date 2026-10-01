"""Tests for daimon.adapters.slack.agent_setup.actions.

The slash command is the one entry that is not a click: it opens a loading
modal on the fresh trigger_id and then replaces it with the Agents view. The
panel's clicks are covered in test_agent_setup_panel_actions.py.

Uses a real Postgres schema (db_session_factory) plus the transport-level
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
from daimon.adapters.slack.agent_setup.actions import handle_agent_setup_command
from daimon.adapters.slack.runtime import SlackRuntime
from daimon.core.github_credentials import build_multifernet, encrypt_token
from daimon.core.stores.slack_bot_tokens import upsert_slack_bot_token
from daimon.testing.factories import make_tenant
from daimon.testing.ma import build_fake_anthropic, make_fake_ma_handler
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_TEAM_ID = "T_ACTIONS_TESTS"
_USER_ID = "U_ACTIONS_TEST"
_CHANNEL_ID = "C_ACTIONS_TEST"
_SLACK_API_BASE = "https://slack.com/api"


async def _seed_team(
    session: AsyncSession,
    *,
    team_id: str = _TEAM_ID,
) -> tuple[uuid.UUID, str, bytes]:
    """Create Tenant + bot token. Returns (tenant_id, fernet_key, encrypted_token)."""
    fernet_key = Fernet.generate_key().decode()
    fernet = build_multifernet((fernet_key,))
    encrypted = encrypt_token(fernet, "xoxb-test")

    tenant = await make_tenant(session, platform="slack", workspace_id=team_id)
    tenant_id = tenant.id
    await upsert_slack_bot_token(session, team_id=team_id, encrypted_token=encrypted)
    await session.flush()
    return tenant_id, fernet_key, encrypted


def _build_runtime(
    fernet_key: str,
    db_factory: async_sessionmaker[AsyncSession],
) -> SlackRuntime:
    """Construct a SlackRuntime with a fake Anthropic transport and real DB factory."""
    settings = MagicMock()
    settings.crypto.keys = (SecretStr(fernet_key),)
    settings.mcp.public_url = None
    settings.mcp.jwt_secret = None
    settings.github = MagicMock()
    settings.github.app_id = None
    return SlackRuntime(
        settings=settings,
        anthropic=build_fake_anthropic(make_fake_ma_handler()),
        sessionmaker=db_factory,
        billing_config=None,
        http_client=MagicMock(spec=httpx.AsyncClient),
        resolver_cache=MagicMock(),  # pyright: ignore[reportArgumentType]  # stub, turn path not exercised
        turn_deps=MagicMock(),  # pyright: ignore[reportArgumentType]  # stub, turn path not exercised
    )


@pytest.mark.asyncio
async def test_handle_agent_setup_command_sends_loading_view_then_update(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: object,
) -> None:
    """handle_agent_setup_command opens a loading modal then updates it with content.

    FakeSlackWebClient intercepts at the aiohttp transport layer — the client
    produced by resolve_web_client uses the same aiohttp session so aioresponses
    catches it automatically.
    """
    _, fernet_key, _ = await _seed_team(db_session)

    runtime = _build_runtime(fernet_key, db_session_factory)
    payload = {
        "team_id": _TEAM_ID,
        "user_id": _USER_ID,
        "channel_id": _CHANNEL_ID,
        "trigger_id": "TRIG_001",
    }

    await handle_agent_setup_command(runtime, payload)

    client_fake: Any = fake_slack_web_client
    open_calls = client_fake.mock.requests.get(("POST", yarl.URL(f"{_SLACK_API_BASE}/views.open")))
    update_calls = client_fake.mock.requests.get(
        ("POST", yarl.URL(f"{_SLACK_API_BASE}/views.update"))
    )
    assert open_calls, "views.open must be called to display the loading modal"
    assert update_calls, "views.update must be called to replace the loading modal with content"
