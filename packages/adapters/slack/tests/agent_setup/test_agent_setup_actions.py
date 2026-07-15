"""Tests for daimon.adapters.slack.agent_setup.actions.

Covers the five required behaviors from 83-05 plan:

- (loading)  handle_agent_setup_command: views.open then views.update sent
- (tab)      tab action sends views.update NOT views.push
- (scope)    workspace-scope with admin calls do_propagate → DB row written +
             views.update sent
- (admin)    mutating action (scope) with non-admin users.info sends NO write
             and posts the ':x: You no longer have permission' ephemeral
- (connect_mcp) agent_setup__connect_mcp sends chat.postEphemeral and does
             NOT call views.update or views.push

All cases use a real Postgres schema (db_session_factory) + the transport-level
FakeSlackWebClient from conftest (no AsyncMock on client.* methods).
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest
import yarl
from cryptography.fernet import Fernet
from daimon.adapters.slack.agent_setup.actions import (
    handle_agent_setup_action,
    handle_agent_setup_command,
)
from daimon.adapters.slack.runtime import SlackRuntime
from daimon.core._models import Tenant
from daimon.core.defaults.metadata import MA_METADATA_KEY_NAME, MA_METADATA_KEY_TENANT
from daimon.core.github_credentials import build_multifernet, encrypt_token
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.scope import ChannelConfigRow, ChannelScopeRef, TenantScopeRef
from daimon.core.stores.scoped_config_read import get_scope
from daimon.core.stores.slack_bot_tokens import upsert_slack_bot_token
from daimon.testing.ma import (
    build_fake_anthropic,
    make_fake_ma_handler,
)
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

_TEAM_ID = "T_ACTIONS_TESTS"
_USER_ID = "U_ACTIONS_TEST"
_VIEW_ID = "V_ACTIONS_TEST"
_CHANNEL_ID = "C_ACTIONS_TEST"
_AGENT_NAME = "test-setup-agent"
_MA_AGENT_ID = f"agent_{'a' * 24}"

_USERS_INFO_PATTERN = re.compile(r"https://slack\.com/api/users\.info.*")
_SLACK_API_BASE = "https://slack.com/api"

_ADMIN_USERS_INFO_PAYLOAD = {
    "ok": True,
    "user": {
        "id": _USER_ID,
        "name": "admin",
        "is_admin": True,
        "is_owner": False,
        "is_primary_owner": False,
    },
}


def _override_users_info_admin(mock: Any) -> None:
    """Replace the conftest non-admin users.info stub with an admin one.

    aioresponses stores matchers by uuid key in insertion order — the first
    matching entry wins.  The conftest registers the non-admin baseline with
    repeat=True before our test adds any override, so a plain .get() append
    never takes effect (the repeat=True entry always matches first).

    This helper removes any existing pattern-matched users.info entries and
    re-registers an admin payload, so the test's is_admin=True flow runs.
    """
    # Collect keys to remove (those whose url_or_pattern matches our pattern)
    to_remove = [
        k
        for k, v in mock._matches.items()  # type: ignore[attr-defined]
        if getattr(v, "url_or_pattern", None) == _USERS_INFO_PATTERN
    ]
    for k in to_remove:
        del mock._matches[k]  # type: ignore[attr-defined]
    # Re-register with admin payload
    mock.get(  # pyright: ignore[reportUnknownMemberType]
        _USERS_INFO_PATTERN,
        payload=_ADMIN_USERS_INFO_PAYLOAD,
        repeat=True,
    )


# ---------------------------------------------------------------------------
# Helpers: DB seeding
# ---------------------------------------------------------------------------


async def _seed_team(
    session: AsyncSession,
    *,
    team_id: str = _TEAM_ID,
) -> tuple[uuid.UUID, str, bytes]:
    """Create Tenant + bot token. Returns (tenant_id, fernet_key, encrypted_token)."""
    fernet_key = Fernet.generate_key().decode()
    fernet = build_multifernet((fernet_key,))
    encrypted = encrypt_token(fernet, "xoxb-test")

    tenant_id = derive_tenant_uuid(platform="slack", workspace_id=team_id)
    session.add(Tenant(id=tenant_id, platform="slack", external_id=team_id))
    await session.flush()
    await upsert_slack_bot_token(session, team_id=team_id, encrypted_token=encrypted)
    await session.flush()
    return tenant_id, fernet_key, encrypted


# ---------------------------------------------------------------------------
# Helpers: fake MA handlers
# ---------------------------------------------------------------------------


def _agent_payload(
    *,
    tenant_id: uuid.UUID,
    agent_name: str = _AGENT_NAME,
    agent_id: str = _MA_AGENT_ID,
) -> dict[str, object]:
    """Build a minimal MA agent payload with correct tenant/name metadata tags."""
    now = datetime.now(UTC).isoformat()
    return {
        "id": agent_id,
        "type": "agent",
        "name": agent_name,
        "version": 1,
        "model": {"id": "claude-sonnet-4-6", "speed": "standard"},
        "system": None,
        "metadata": {
            MA_METADATA_KEY_TENANT: str(tenant_id),
            MA_METADATA_KEY_NAME: agent_name,
        },
        "mcp_servers": [],
        "tools": [],
        "skills": [],
        "created_at": now,
        "updated_at": now,
        "archived_at": None,
        "description": None,
    }


def _make_ma_handler_with_agents(
    agents: list[dict[str, object]],
) -> Any:
    """Return an httpx handler that serves a fixed list of agents on GET /v1/agents."""
    agent_store: dict[str, dict[str, object]] = {
        str(ag["id"]): ag
        for ag in agents  # type: ignore[index]
    }

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        method = request.method

        if method == "GET" and path == "/v1/agents":
            return httpx.Response(
                200,
                json={"data": list(agent_store.values()), "has_more": False},
            )
        if method == "GET" and path.startswith("/v1/agents/"):
            agent_id_req = path.removeprefix("/v1/agents/")
            if agent_id_req in agent_store:
                return httpx.Response(200, json=agent_store[agent_id_req])
            return httpx.Response(
                404,
                json={
                    "type": "error",
                    "error": {"type": "not_found_error", "message": "not found"},
                },
            )
        # environments (used by some agent_setup read paths)
        if method == "GET" and path == "/v1/environments":
            return httpx.Response(200, json={"data": [], "has_more": False})

        return httpx.Response(404, json={"error": f"unhandled {method} {path}"})

    return handler


# ---------------------------------------------------------------------------
# Helpers: runtime construction
# ---------------------------------------------------------------------------


def _build_runtime(
    fernet_key: str,
    db_factory: async_sessionmaker[AsyncSession],
    *,
    anthropic_handler: Any = None,
) -> SlackRuntime:
    """Construct a SlackRuntime with a fake Anthropic transport and real DB factory."""
    handler = anthropic_handler or make_fake_ma_handler()
    settings = MagicMock()
    settings.crypto.keys = (SecretStr(fernet_key),)
    settings.mcp.public_url = None  # disabled by default; override per test
    settings.mcp.jwt_secret = None
    settings.github = MagicMock()
    settings.github.app_id = None
    settings.slack.dev_allow_all_admin = False  # real default; MagicMock would be truthy
    return SlackRuntime(
        settings=settings,
        anthropic=build_fake_anthropic(handler),
        sessionmaker=db_factory,
        http_client=MagicMock(spec=httpx.AsyncClient),
    )


# ---------------------------------------------------------------------------
# Payload builders
# ---------------------------------------------------------------------------


def _command_payload(
    *,
    team_id: str = _TEAM_ID,
    user_id: str = _USER_ID,
    channel_id: str = _CHANNEL_ID,
    trigger_id: str = "TRIG_001",
) -> dict[str, object]:
    return {
        "team_id": team_id,
        "user_id": user_id,
        "channel_id": channel_id,
        "trigger_id": trigger_id,
    }


def _action_payload(
    action_id: str,
    *,
    team_id: str = _TEAM_ID,
    user_id: str = _USER_ID,
    view_id: str = _VIEW_ID,
    channel_id: str = _CHANNEL_ID,
    selected_agent_name: str | None = None,
    active_section: str = "agent",
    action_extra: dict[str, object] | None = None,
) -> dict[str, object]:
    """Build a generic block_actions payload."""
    meta = json.dumps(
        {
            "team_id": team_id,
            "channel_id": channel_id,
            "selected_agent_name": selected_agent_name,
            "agent_name": selected_agent_name,
            "active_section": active_section,
            "parent_section": active_section,
        }
    )
    action: dict[str, object] = {"action_id": action_id}
    if action_extra:
        action.update(action_extra)
    return {
        "team": {"id": team_id},
        "user": {"id": user_id},
        "view": {"id": view_id, "private_metadata": meta},
        "actions": [action],
    }


# ---------------------------------------------------------------------------
# Test: (loading) — views.open then views.update on handle_agent_setup_command
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_agent_setup_command_sends_loading_view_then_update(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: object,
) -> None:
    """handle_agent_setup_command opens a loading modal then updates it with content (D-06).

    FakeSlackWebClient intercepts at the aiohttp transport layer — the client
    produced by resolve_web_client uses the same aiohttp session so aioresponses
    catches it automatically.
    """
    _, fernet_key, _ = await _seed_team(db_session)

    runtime = _build_runtime(fernet_key, db_session_factory)
    payload = _command_payload()

    await handle_agent_setup_command(runtime, payload)  # type: ignore[arg-type]

    client_fake: Any = fake_slack_web_client
    open_calls = client_fake.mock.requests.get(("POST", yarl.URL(f"{_SLACK_API_BASE}/views.open")))
    update_calls = client_fake.mock.requests.get(
        ("POST", yarl.URL(f"{_SLACK_API_BASE}/views.update"))
    )
    assert open_calls, "views.open must be called to display the loading modal (D-06)"
    assert update_calls, (
        "views.update must be called to replace the loading modal with content (D-06)"
    )


# ---------------------------------------------------------------------------
# Test: (tab) — tab action calls views.update, NEVER views.push
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_agent_setup_action_tab_swap_calls_views_update_not_push(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: object,
) -> None:
    """Tab-swap actions must update the modal in-place (views.update), never push.

    This enforces the 3-level modal cap structural guarantee — tabs swap in-place.
    """
    tenant_id, fernet_key, _ = await _seed_team(db_session)

    # Seed an agent in the fake MA so tab swap doesn't hit stale path
    agent_payload = _agent_payload(tenant_id=tenant_id)
    handler = _make_ma_handler_with_agents([agent_payload])
    runtime = _build_runtime(fernet_key, db_session_factory, anthropic_handler=handler)

    payload = _action_payload(
        "agent_setup__tab:skills",
        selected_agent_name=_AGENT_NAME,
    )

    await handle_agent_setup_action(runtime, payload)  # type: ignore[arg-type]

    client_fake: Any = fake_slack_web_client
    push_calls = client_fake.mock.requests.get(("POST", yarl.URL(f"{_SLACK_API_BASE}/views.push")))
    update_calls = client_fake.mock.requests.get(
        ("POST", yarl.URL(f"{_SLACK_API_BASE}/views.update"))
    )
    assert not push_calls, (
        "tab swap must NEVER call views.push — only the edit branch may push (3-level modal cap)"
    )
    assert update_calls, "tab swap must call views.update to swap in-place"


# ---------------------------------------------------------------------------
# Test: (scope) — workspace scope with admin writes scope row and calls views.update
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_agent_setup_action_scope_workspace_with_admin_writes_scope_row(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: object,
) -> None:
    """Workspace-scope click from an admin calls do_propagate → DB row written + views.update.

    Override users.info to return is_admin=True (admin baseline). Confirm:
    1. The scope row exists in Postgres after the action.
    2. views.update was called to re-render L1.
    """
    tenant_id, fernet_key, _ = await _seed_team(db_session)

    agent_payload = _agent_payload(tenant_id=tenant_id)
    handler = _make_ma_handler_with_agents([agent_payload])
    runtime = _build_runtime(fernet_key, db_session_factory, anthropic_handler=handler)

    # Override aioresponses to return is_admin=True for this test.
    # Must clear the conftest non-admin entry first (repeat=True prevents later
    # entries from ever being reached).
    client_fake: Any = fake_slack_web_client
    _override_users_info_admin(client_fake.mock)

    payload = _action_payload(
        "agent_setup__scope:workspace",
        selected_agent_name=_AGENT_NAME,
    )

    await handle_agent_setup_action(runtime, payload)  # type: ignore[arg-type]

    # Assert the scope row was written to Postgres
    async with db_session_factory() as session:
        row = await get_scope(session, scope=TenantScopeRef(tenant_id=tenant_id))

    from daimon.core.scope import TenantConfigRow

    assert isinstance(row, TenantConfigRow), (
        "do_propagate should write a TenantConfigRow for the workspace scope"
    )
    assert row.agent_name == _AGENT_NAME, "propagated agent_name must match the selected agent"

    # Assert views.update was called to re-render L1
    update_calls = client_fake.mock.requests.get(
        ("POST", yarl.URL(f"{_SLACK_API_BASE}/views.update"))
    )
    assert update_calls, "views.update must be called after a successful scope write"


# ---------------------------------------------------------------------------
# Test: (admin) — non-admin mutating action sends ephemeral, no DB write
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_agent_setup_action_scope_workspace_with_non_admin_sends_ephemeral_no_write(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: object,
) -> None:
    """Non-admin scope attempt must send ':x: no permission' ephemeral and make no DB write.

    The default FakeSlackWebClient users.info returns is_admin=False — fail-closed
    baseline. No additional override needed.
    """
    tenant_id, fernet_key, _ = await _seed_team(db_session)

    runtime = _build_runtime(fernet_key, db_session_factory)
    payload = _action_payload(
        "agent_setup__scope:workspace",
        selected_agent_name=_AGENT_NAME,
    )

    await handle_agent_setup_action(runtime, payload)  # type: ignore[arg-type]

    # Assert NO scope row was written
    async with db_session_factory() as session:
        row = await get_scope(session, scope=TenantScopeRef(tenant_id=tenant_id))
    assert row is None, "non-admin scope attempt must not write a DB row (fail-closed)"

    # Assert the ':x: no permission' ephemeral was sent
    client_fake: Any = fake_slack_web_client
    ephemeral_calls = client_fake.mock.requests.get(
        ("POST", yarl.URL(f"{_SLACK_API_BASE}/chat.postEphemeral"))
    )
    assert ephemeral_calls, "non-admin action must send a ':x: no permission' ephemeral"
    # Inspect the JSON body for the expected text.
    # Slack SDK sends JSON as kwargs["json"] (not "data") to the underlying
    # aiohttp session — aioresponses captures this in call.kwargs["json"].
    last_call = ephemeral_calls[-1]
    body_json: dict[str, Any] = last_call.kwargs.get("json") or {}
    body_text: str = body_json.get("text") or ""
    assert "no longer have permission" in body_text, (
        "ephemeral text should mention lack of permission (fail-closed re-check)"
    )


# ---------------------------------------------------------------------------
# Test: (connect_mcp) — sends chat.postEphemeral, does NOT call views.update/push
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_agent_setup_action_connect_mcp_sends_ephemeral_not_modal_update(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: object,
) -> None:
    """connect_mcp: sends chat.postEphemeral (the config snippet), modal stays OPEN.

    Per D-10: spill-outs are ephemeral; no views.update or views.push is sent.
    Requires MCP public_url + jwt_secret configured on settings.
    """
    tenant_id, fernet_key, _ = await _seed_team(db_session)

    agent_payload = _agent_payload(tenant_id=tenant_id)
    handler = _make_ma_handler_with_agents([agent_payload])

    settings = MagicMock()
    settings.crypto.keys = (SecretStr(fernet_key),)
    settings.mcp.public_url = "https://mcp.example.com"
    settings.mcp.jwt_secret = SecretStr("test-secret-32-bytes-long-padding!")
    settings.github = MagicMock()
    settings.github.app_id = None
    settings.slack.dev_allow_all_admin = False  # real default; MagicMock would be truthy
    runtime = SlackRuntime(
        settings=settings,
        anthropic=build_fake_anthropic(handler),
        sessionmaker=db_session_factory,
        http_client=MagicMock(spec=httpx.AsyncClient),
    )

    # Override aioresponses to return is_admin=True (connect_mcp re-checks admin).
    client_fake: Any = fake_slack_web_client
    _override_users_info_admin(client_fake.mock)

    payload = _action_payload(
        "agent_setup__connect_mcp",
        selected_agent_name=_AGENT_NAME,
    )

    await handle_agent_setup_action(runtime, payload)  # type: ignore[arg-type]

    # Assert chat.postEphemeral was sent (the MCP config snippet)
    ephemeral_calls = client_fake.mock.requests.get(
        ("POST", yarl.URL(f"{_SLACK_API_BASE}/chat.postEphemeral"))
    )
    assert ephemeral_calls, "connect_mcp must send a chat.postEphemeral with the MCP config"

    # Assert the modal was NOT updated or pushed (D-10: modal stays open)
    update_calls = client_fake.mock.requests.get(
        ("POST", yarl.URL(f"{_SLACK_API_BASE}/views.update"))
    )
    push_calls = client_fake.mock.requests.get(("POST", yarl.URL(f"{_SLACK_API_BASE}/views.push")))
    assert not update_calls, "connect_mcp must NOT call views.update — modal stays open (D-10)"
    assert not push_calls, "connect_mcp must NOT call views.push — modal stays open (D-10)"


# ---------------------------------------------------------------------------
# Helpers for L3 form-open route tests (83-09)
# ---------------------------------------------------------------------------


def _get_push_callback_id(client_fake: Any) -> str | None:
    """Extract callback_id from the most recent views.push call body, or None."""
    push_calls = client_fake.mock.requests.get(
        ("POST", yarl.URL(f"{_SLACK_API_BASE}/views.push")), []
    )
    if not push_calls:
        return None
    body: dict[str, Any] = push_calls[-1].kwargs.get("json") or {}
    view: dict[str, Any] = body.get("view") or {}
    return str(view.get("callback_id")) if view.get("callback_id") else None


# ---------------------------------------------------------------------------
# Tests: L3 form-open routes (83-09) — admin pushes the correct form
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_agent_setup_action_new_pushes_new_agent_form(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: object,
) -> None:
    """agent_setup__new from an admin must views_push callback_id 'agent_setup__new_agent'."""
    _, fernet_key, _ = await _seed_team(db_session)
    runtime = _build_runtime(fernet_key, db_session_factory)

    client_fake: Any = fake_slack_web_client
    _override_users_info_admin(client_fake.mock)

    payload = _action_payload("agent_setup__new")
    await handle_agent_setup_action(runtime, payload)  # type: ignore[arg-type]

    callback_id = _get_push_callback_id(client_fake)
    assert callback_id == "agent_setup__new_agent", (
        "agent_setup__new must push the new-agent form with callback_id='agent_setup__new_agent', "
        f"got {callback_id!r}"
    )


@pytest.mark.asyncio
async def test_handle_agent_setup_action_fork_pushes_fork_agent_form(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: object,
) -> None:
    """agent_setup__fork from an admin with a valid agent must push callback_id 'agent_setup__fork_agent'."""
    tenant_id, fernet_key, _ = await _seed_team(db_session)
    agent_payload = _agent_payload(tenant_id=tenant_id)
    handler = _make_ma_handler_with_agents([agent_payload])
    runtime = _build_runtime(fernet_key, db_session_factory, anthropic_handler=handler)

    client_fake: Any = fake_slack_web_client
    _override_users_info_admin(client_fake.mock)

    payload = _action_payload("agent_setup__fork", selected_agent_name=_AGENT_NAME)
    await handle_agent_setup_action(runtime, payload)  # type: ignore[arg-type]

    callback_id = _get_push_callback_id(client_fake)
    assert callback_id == "agent_setup__fork_agent", (
        "agent_setup__fork must push the fork-agent form with callback_id='agent_setup__fork_agent', "
        f"got {callback_id!r}"
    )


@pytest.mark.asyncio
async def test_handle_agent_setup_action_edit_agent_form_pushes_edit_agent_form(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: object,
) -> None:
    """agent_setup__edit_agent_form from an admin must push callback_id 'agent_setup__edit_agent'."""
    tenant_id, fernet_key, _ = await _seed_team(db_session)
    agent_payload = _agent_payload(tenant_id=tenant_id)
    handler = _make_ma_handler_with_agents([agent_payload])
    runtime = _build_runtime(fernet_key, db_session_factory, anthropic_handler=handler)

    client_fake: Any = fake_slack_web_client
    _override_users_info_admin(client_fake.mock)

    payload = _action_payload(
        "agent_setup__edit_agent_form",
        selected_agent_name=_AGENT_NAME,
        active_section="agent",
    )
    await handle_agent_setup_action(runtime, payload)  # type: ignore[arg-type]

    callback_id = _get_push_callback_id(client_fake)
    assert callback_id == "agent_setup__edit_agent", (
        "agent_setup__edit_agent_form must push the edit-agent form with "
        "callback_id='agent_setup__edit_agent', "
        f"got {callback_id!r}"
    )


@pytest.mark.asyncio
async def test_handle_agent_setup_action_edit_repo_form_pushes_edit_repo_form(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: object,
) -> None:
    """agent_setup__edit_repo_form from an admin must push callback_id 'agent_setup__edit_repo'."""
    tenant_id, fernet_key, _ = await _seed_team(db_session)
    agent_payload = _agent_payload(tenant_id=tenant_id)
    handler = _make_ma_handler_with_agents([agent_payload])
    runtime = _build_runtime(fernet_key, db_session_factory, anthropic_handler=handler)

    client_fake: Any = fake_slack_web_client
    _override_users_info_admin(client_fake.mock)

    payload = _action_payload(
        "agent_setup__edit_repo_form",
        selected_agent_name=_AGENT_NAME,
        active_section="repo_auth",
    )
    await handle_agent_setup_action(runtime, payload)  # type: ignore[arg-type]

    callback_id = _get_push_callback_id(client_fake)
    assert callback_id == "agent_setup__edit_repo", (
        "agent_setup__edit_repo_form must push the edit-repo form with "
        "callback_id='agent_setup__edit_repo', "
        f"got {callback_id!r}"
    )


@pytest.mark.asyncio
async def test_handle_agent_setup_action_add_skill_pushes_add_skill_form(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: object,
) -> None:
    """agent_setup__add_skill from an admin must push callback_id 'agent_setup__add_skill'."""
    tenant_id, fernet_key, _ = await _seed_team(db_session)
    agent_payload = _agent_payload(tenant_id=tenant_id)
    handler = _make_ma_handler_with_agents([agent_payload])
    runtime = _build_runtime(fernet_key, db_session_factory, anthropic_handler=handler)

    client_fake: Any = fake_slack_web_client
    _override_users_info_admin(client_fake.mock)

    payload = _action_payload(
        "agent_setup__add_skill",
        selected_agent_name=_AGENT_NAME,
        active_section="skills",
    )
    await handle_agent_setup_action(runtime, payload)  # type: ignore[arg-type]

    callback_id = _get_push_callback_id(client_fake)
    assert callback_id == "agent_setup__add_skill", (
        "agent_setup__add_skill must push the add-skill form with "
        "callback_id='agent_setup__add_skill', "
        f"got {callback_id!r}"
    )


@pytest.mark.asyncio
async def test_handle_agent_setup_action_add_mcp_pushes_add_mcp_form(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: object,
) -> None:
    """agent_setup__add_mcp from an admin must push callback_id 'agent_setup__add_mcp'."""
    tenant_id, fernet_key, _ = await _seed_team(db_session)
    agent_payload = _agent_payload(tenant_id=tenant_id)
    handler = _make_ma_handler_with_agents([agent_payload])
    runtime = _build_runtime(fernet_key, db_session_factory, anthropic_handler=handler)

    client_fake: Any = fake_slack_web_client
    _override_users_info_admin(client_fake.mock)

    payload = _action_payload(
        "agent_setup__add_mcp",
        selected_agent_name=_AGENT_NAME,
        active_section="mcps",
    )
    await handle_agent_setup_action(runtime, payload)  # type: ignore[arg-type]

    callback_id = _get_push_callback_id(client_fake)
    assert callback_id == "agent_setup__add_mcp", (
        "agent_setup__add_mcp must push the add-mcp form with "
        "callback_id='agent_setup__add_mcp', "
        f"got {callback_id!r}"
    )


@pytest.mark.asyncio
async def test_handle_agent_setup_action_paste_secrets_pushes_paste_secrets_form(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: object,
) -> None:
    """agent_setup__paste_secrets from an admin must push callback_id 'agent_setup__paste_secrets'."""
    tenant_id, fernet_key, _ = await _seed_team(db_session)
    agent_payload = _agent_payload(tenant_id=tenant_id)
    handler = _make_ma_handler_with_agents([agent_payload])
    runtime = _build_runtime(fernet_key, db_session_factory, anthropic_handler=handler)

    client_fake: Any = fake_slack_web_client
    _override_users_info_admin(client_fake.mock)

    payload = _action_payload(
        "agent_setup__paste_secrets",
        selected_agent_name=_AGENT_NAME,
        active_section="secrets",
    )
    await handle_agent_setup_action(runtime, payload)  # type: ignore[arg-type]

    callback_id = _get_push_callback_id(client_fake)
    assert callback_id == "agent_setup__paste_secrets", (
        "agent_setup__paste_secrets must push the paste-secrets form with "
        "callback_id='agent_setup__paste_secrets', "
        f"got {callback_id!r}"
    )


# ---------------------------------------------------------------------------
# Test: non-admin agent_setup__new is refused with no views.push (T-83-20)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_agent_setup_action_new_with_non_admin_sends_ephemeral_no_push(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: object,
) -> None:
    """Non-admin agent_setup__new must send the permission-refused ephemeral and NOT views_push.

    The default FakeSlackWebClient users.info returns is_admin=False (fail-closed baseline).
    T-83-20: every new L3-open branch re-resolves is_admin and refuses on False.
    """
    _, fernet_key, _ = await _seed_team(db_session)
    runtime = _build_runtime(fernet_key, db_session_factory)

    payload = _action_payload("agent_setup__new")
    await handle_agent_setup_action(runtime, payload)  # type: ignore[arg-type]

    client_fake: Any = fake_slack_web_client
    push_calls = client_fake.mock.requests.get(
        ("POST", yarl.URL(f"{_SLACK_API_BASE}/views.push")), []
    )
    assert not push_calls, (
        "non-admin agent_setup__new must NOT call views.push (T-83-20 admin gate)"
    )

    ephemeral_calls = client_fake.mock.requests.get(
        ("POST", yarl.URL(f"{_SLACK_API_BASE}/chat.postEphemeral")), []
    )
    assert ephemeral_calls, (
        "non-admin agent_setup__new must send a permission-refused ephemeral (T-83-20)"
    )
    last_call = ephemeral_calls[-1]
    body_json: dict[str, Any] = last_call.kwargs.get("json") or {}
    body_text: str = body_json.get("text") or ""
    assert "no longer have permission" in body_text, (
        "ephemeral text must mention lack of permission (T-83-20 fail-closed re-check)"
    )


@pytest.mark.asyncio
async def test_handle_agent_setup_action_new_with_non_admin_but_dev_allow_all_pushes_form(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: object,
) -> None:
    """dev_allow_all_admin opens the gate for a non-admin: agent_setup__new pushes the form.

    The default FakeSlackWebClient users.info returns is_admin=False, so the only
    reason the new-agent form opens is the DAIMON_SLACK__DEV_ALLOW_ALL_ADMIN
    override threaded from settings — the testing escape hatch.
    """
    _, fernet_key, _ = await _seed_team(db_session)
    runtime = _build_runtime(fernet_key, db_session_factory)
    runtime.settings.slack.dev_allow_all_admin = True  # type: ignore[attr-defined]  # MagicMock settings

    payload = _action_payload("agent_setup__new")
    await handle_agent_setup_action(runtime, payload)  # type: ignore[arg-type]

    client_fake: Any = fake_slack_web_client
    callback_id = _get_push_callback_id(client_fake)
    assert callback_id == "agent_setup__new_agent", (
        f"non-admin with dev_allow_all_admin=True must push the new-agent form, got {callback_id!r}"
    )


# ---------------------------------------------------------------------------
# Test: scope:channel — selected_channel != invoking channel_id
# CR-03: hint must reference the selected (persisted) channel, not the invoking one
# ---------------------------------------------------------------------------

_SELECTED_CHANNEL = "C_SELECTED_CHANNEL"  # differs from _CHANNEL_ID ("C_ACTIONS_TEST")


@pytest.mark.asyncio
async def test_handle_agent_setup_action_scope_channel_writes_selected_channel_and_hint_matches(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: object,
) -> None:
    """scope:channel writes to selected_channel and the rendered scope_hint names selected_channel.

    This is the CR-03 regression test: previously load_scope_hint was called with
    channel_id (the invoking channel), so the hint would show the invoking channel
    even though the write went to selected_channel. After the fix the hint reads
    for selected_channel, making the displayed scope match the persisted scope.

    Assertions:
      (a) The DB ChannelScopeRow is written for _SELECTED_CHANNEL (not _CHANNEL_ID).
      (b) The views.update L1 payload contains a context block whose text references
          _SELECTED_CHANNEL (":hash: Set for *#C_SELECTED_CHANNEL*").
    """
    tenant_id, fernet_key, _ = await _seed_team(db_session)

    agent_payload = _agent_payload(tenant_id=tenant_id)
    handler = _make_ma_handler_with_agents([agent_payload])
    runtime = _build_runtime(fernet_key, db_session_factory, anthropic_handler=handler)

    client_fake: Any = fake_slack_web_client
    _override_users_info_admin(client_fake.mock)

    # Build a scope:channel payload where selected_channel differs from channel_id.
    payload = _action_payload(
        "agent_setup__scope:channel",
        selected_agent_name=_AGENT_NAME,
        channel_id=_CHANNEL_ID,
        action_extra={"selected_channel": _SELECTED_CHANNEL},
    )

    await handle_agent_setup_action(runtime, payload)  # type: ignore[arg-type]

    # (a) DB write went to the SELECTED channel, not the invoking channel.
    async with db_session_factory() as session:
        selected_row = await get_scope(
            session,
            scope=ChannelScopeRef(tenant_id=tenant_id, channel_id=_SELECTED_CHANNEL),
        )
        invoking_row = await get_scope(
            session,
            scope=ChannelScopeRef(tenant_id=tenant_id, channel_id=_CHANNEL_ID),
        )

    assert isinstance(selected_row, ChannelConfigRow), (
        f"do_propagate must write a ChannelConfigRow for the SELECTED channel "
        f"({_SELECTED_CHANNEL}), not the invoking channel ({_CHANNEL_ID})"
    )
    assert selected_row.agent_name == _AGENT_NAME, (
        f"propagated agent_name must match the selected agent, got {selected_row.agent_name!r}"
    )
    assert invoking_row is None, (
        f"no row must be written for the invoking channel ({_CHANNEL_ID}); "
        f"the write must target only selected_channel"
    )

    # (b) The scope_hint in the rendered L1 references the selected channel.
    update_calls = client_fake.mock.requests.get(
        ("POST", yarl.URL(f"{_SLACK_API_BASE}/views.update")), []
    )
    assert update_calls, "views.update must be called after scope:channel write"
    update_body: dict[str, Any] = update_calls[-1].kwargs.get("json") or {}
    rendered_view: dict[str, Any] = update_body.get("view") or {}
    # Walk all blocks looking for a context block whose mrkdwn text references selected_channel
    scope_hint_found = False
    for block in rendered_view.get("blocks", []):
        if block.get("type") != "context":
            continue
        for element in block.get("elements", []):
            text_val: str = element.get("text") or ""
            if f"#{_SELECTED_CHANNEL}" in text_val:
                scope_hint_found = True
    assert scope_hint_found, (
        f"The rendered L1 view must contain a context block referencing "
        f"#{_SELECTED_CHANNEL} (the selected/persisted channel), not #{_CHANNEL_ID} "
        f"(the invoking channel) — CR-03 regression guard"
    )
