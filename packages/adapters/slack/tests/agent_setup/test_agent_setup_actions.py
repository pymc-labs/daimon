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

Also covers the field-follows-the-gate authorization matrix (10-06): the
branches open to every caller (new/fork/edit/paste_secrets), the branches
gated on shared state by ``refuse_if_shared_and_not_admin``
(edit_repo_form/remove_secret),
the admin-only-unconditional branches (delete/scope:*/connect_mcp), and the
field-conditional branches gated by ``refuse_if_reachable_and_not_admin``
(remove_skill/remove_mcp/edit_agent_form/add_skill/add_mcp) — each proved for
a non-admin caller against a real reachable-vs-unreachable agent, written via
``scoped_config_write.set_fields``, never by patching the predicate.

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
from anthropic.types.beta import BetaManagedAgentsAgent, BetaManagedAgentsModelConfig
from cryptography.fernet import Fernet
from daimon.adapters.slack.agent_setup.actions import (
    handle_agent_setup_action,
    handle_agent_setup_command,
)
from daimon.adapters.slack.runtime import SlackRuntime
from daimon.core.defaults.metadata import (
    MA_METADATA_KEY_MANAGED,
    MA_METADATA_KEY_NAME,
    MA_METADATA_KEY_TENANT,
)
from daimon.core.github_credentials import build_multifernet, encrypt_token
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.scope import ChannelConfigRow, ChannelScopeRef, TenantScopeRef
from daimon.core.stores.agent_files import get_agent_file, put_agent_file
from daimon.core.stores.scoped_config_read import get_scope
from daimon.core.stores.scoped_config_write import set_fields
from daimon.core.stores.slack_bot_tokens import upsert_slack_bot_token
from daimon.testing.factories import make_tenant
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

    tenant = await make_tenant(session, platform="slack", workspace_id=team_id)
    tenant_id = tenant.id
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


def _make_ma_handler_with_agents_and_update(
    agents: list[dict[str, object]],
) -> Any:
    """Like ``_make_ma_handler_with_agents``, plus PATCH/POST update on
    ``/v1/agents/{id}`` — needed by removal-write branches (remove_skill,
    remove_mcp) that call ``update_agent_with_version_retry``."""
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
        m = re.match(r"^/v1/agents/(?P<id>[^/]+)$", path)
        if m and method == "GET":
            agent_id_req = m.group("id")
            if agent_id_req in agent_store:
                return httpx.Response(200, json=agent_store[agent_id_req])
            return httpx.Response(
                404,
                json={
                    "type": "error",
                    "error": {"type": "not_found_error", "message": "not found"},
                },
            )
        if m and method in {"PATCH", "POST"}:
            agent_id_req = m.group("id")
            if agent_id_req not in agent_store:
                return httpx.Response(
                    404,
                    json={
                        "type": "error",
                        "error": {"type": "not_found_error", "message": "not found"},
                    },
                )
            body: dict[str, Any] = json.loads(request.content)
            existing = agent_store[agent_id_req]
            merged: dict[str, object] = {**existing, **body}
            merged["version"] = int(existing.get("version", 1)) + 1  # type: ignore[arg-type]
            agent_store[agent_id_req] = merged
            return httpx.Response(200, json=merged)
        if method == "GET" and path == "/v1/environments":
            return httpx.Response(200, json={"data": [], "has_more": False})

        return httpx.Response(404, json={"error": f"unhandled {method} {path}"})

    return handler


async def _mark_reachable(
    db_session_factory: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    agent_name: str,
) -> None:
    """Write a real tenant-scope propagation row so the named agent is
    currently reachable — never patch the reachability predicate."""
    async with db_session_factory() as session, session.begin():
        await set_fields(
            session,
            scope=TenantScopeRef(tenant_id=tenant_id),
            tenant_id=tenant_id,
            agent_name=agent_name,
            mode="agent",
        )


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
    return SlackRuntime(
        settings=settings,
        anthropic=build_fake_anthropic(handler),
        sessionmaker=db_factory,
        billing_config=None,
        http_client=MagicMock(spec=httpx.AsyncClient),
        resolver_cache=MagicMock(),  # pyright: ignore[reportArgumentType]  # stub, turn path not exercised
        turn_deps=MagicMock(),  # pyright: ignore[reportArgumentType]  # stub, turn path not exercised
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
    """handle_agent_setup_command opens a loading modal then updates it with content.

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
    assert open_calls, "views.open must be called to display the loading modal"
    assert update_calls, "views.update must be called to replace the loading modal with content"


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

    Spill-outs are ephemeral; no views.update or views.push is sent.
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
    runtime = SlackRuntime(
        settings=settings,
        anthropic=build_fake_anthropic(handler),
        sessionmaker=db_session_factory,
        billing_config=None,
        http_client=MagicMock(spec=httpx.AsyncClient),
        resolver_cache=MagicMock(),  # pyright: ignore[reportArgumentType]  # stub, turn path not exercised
        turn_deps=MagicMock(),  # pyright: ignore[reportArgumentType]  # stub, turn path not exercised
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

    # Assert the modal was NOT updated or pushed (modal stays open)
    update_calls = client_fake.mock.requests.get(
        ("POST", yarl.URL(f"{_SLACK_API_BASE}/views.update"))
    )
    push_calls = client_fake.mock.requests.get(("POST", yarl.URL(f"{_SLACK_API_BASE}/views.push")))
    assert not update_calls, "connect_mcp must NOT call views.update — modal stays open"
    assert not push_calls, "connect_mcp must NOT call views.push — modal stays open"


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
# Test: agent_setup__new is open to every member, admin or not
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_agent_setup_action_new_with_non_admin_pushes_form_no_ephemeral(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: object,
) -> None:
    """agent_setup__new is always-open: a non-admin still gets the form pushed,
    with no refusal ephemeral. Building an unscoped agent is not tenant-wide blast radius.

    The default FakeSlackWebClient users.info returns is_admin=False (fail-closed baseline
    for OTHER, still-gated branches) -- this branch no longer checks admin at all.
    """
    _, fernet_key, _ = await _seed_team(db_session)
    runtime = _build_runtime(fernet_key, db_session_factory)

    payload = _action_payload("agent_setup__new")
    await handle_agent_setup_action(runtime, payload)  # type: ignore[arg-type]

    client_fake: Any = fake_slack_web_client
    callback_id = _get_push_callback_id(client_fake)
    assert callback_id == "agent_setup__new_agent", (
        f"non-admin agent_setup__new must push the new-agent form, got {callback_id!r}"
    )

    ephemeral_calls = client_fake.mock.requests.get(
        ("POST", yarl.URL(f"{_SLACK_API_BASE}/chat.postEphemeral")), []
    )
    assert not ephemeral_calls, (
        "non-admin agent_setup__new must NOT send a refusal ephemeral (always open)"
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


# ---------------------------------------------------------------------------
# Assertion helpers for the field-follows-the-gate matrix (10-06)
# ---------------------------------------------------------------------------


def _ephemeral_texts(client_fake: Any) -> list[str]:
    calls = client_fake.mock.requests.get(
        ("POST", yarl.URL(f"{_SLACK_API_BASE}/chat.postEphemeral")), []
    )
    texts: list[str] = []
    for call in calls:
        body: dict[str, Any] = call.kwargs.get("json") or {}
        texts.append(str(body.get("text") or ""))
    return texts


def _has_push(client_fake: Any) -> bool:
    return bool(
        client_fake.mock.requests.get(("POST", yarl.URL(f"{_SLACK_API_BASE}/views.push")), [])
    )


def _has_update(client_fake: Any) -> bool:
    return bool(
        client_fake.mock.requests.get(("POST", yarl.URL(f"{_SLACK_API_BASE}/views.update")), [])
    )


def _assert_no_refusal(client_fake: Any) -> None:
    texts = _ephemeral_texts(client_fake)
    assert not any("no longer have permission" in t for t in texts), (
        f"must NOT post the permission-refusal ephemeral, got ephemerals: {texts!r}"
    )


def _assert_reachability_refusal(client_fake: Any) -> None:
    texts = _ephemeral_texts(client_fake)
    assert any("workspace-admin" in t for t in texts), (
        f"must post the reachability-refusal ephemeral naming why, got ephemerals: {texts!r}"
    )


# ---------------------------------------------------------------------------
# Branches open to every caller: new, fork, edit, and the paste_secrets form
# push -- the last of those is refused at submission rather than at the push,
# so its open-time test below asserts the push on purpose. `new` is covered
# above; fork, edit, and paste_secrets follow.
#
# Branches gated on shared state via refuse_if_shared_and_not_admin:
# edit_repo_form and remove_secret. Each has a non-admin refusal test against a
# workspace-default agent and an admin-positive sibling proving the same action
# still succeeds for an admin.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_agent_setup_action_fork_non_admin_pushes_form_no_ephemeral(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: object,
) -> None:
    tenant_id, fernet_key, _ = await _seed_team(db_session)
    agent_payload = _agent_payload(tenant_id=tenant_id)
    handler = _make_ma_handler_with_agents([agent_payload])
    runtime = _build_runtime(fernet_key, db_session_factory, anthropic_handler=handler)

    payload = _action_payload("agent_setup__fork", selected_agent_name=_AGENT_NAME)
    await handle_agent_setup_action(runtime, payload)  # type: ignore[arg-type]

    client_fake: Any = fake_slack_web_client
    assert _get_push_callback_id(client_fake) == "agent_setup__fork_agent", (
        "non-admin agent_setup__fork must push the fork-agent form"
    )
    _assert_no_refusal(client_fake)


@pytest.mark.asyncio
async def test_handle_agent_setup_action_edit_non_admin_pushes_l2_no_ephemeral(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: object,
) -> None:
    tenant_id, fernet_key, _ = await _seed_team(db_session)
    agent_payload = _agent_payload(tenant_id=tenant_id)
    handler = _make_ma_handler_with_agents([agent_payload])
    runtime = _build_runtime(fernet_key, db_session_factory, anthropic_handler=handler)

    payload = _action_payload("agent_setup__edit", selected_agent_name=_AGENT_NAME)
    await handle_agent_setup_action(runtime, payload)  # type: ignore[arg-type]

    client_fake: Any = fake_slack_web_client
    assert _has_push(client_fake), "non-admin agent_setup__edit must push L2"
    _assert_no_refusal(client_fake)


@pytest.mark.asyncio
async def test_handle_agent_setup_action_edit_repo_form_non_admin_on_default_agent_refuses_with_ephemeral(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: object,
) -> None:
    """A non-admin never sees the edit-repo form for the workspace's default agent.

    The form prompts for a GitHub personal access token, so the refusal has to
    land at the push -- once the form is on screen the member has already been
    asked for a credential the submission would refuse to use.
    """
    tenant_id, fernet_key, _ = await _seed_team(db_session)
    agent_payload = _agent_payload(tenant_id=tenant_id)
    handler = _make_ma_handler_with_agents([agent_payload])
    runtime = _build_runtime(fernet_key, db_session_factory, anthropic_handler=handler)
    await _mark_reachable(db_session_factory, tenant_id=tenant_id, agent_name=_AGENT_NAME)

    payload = _action_payload(
        "agent_setup__edit_repo_form", selected_agent_name=_AGENT_NAME, active_section="repo_auth"
    )
    await handle_agent_setup_action(runtime, payload)  # type: ignore[arg-type]

    client_fake: Any = fake_slack_web_client
    assert not _has_push(client_fake), (
        "non-admin agent_setup__edit_repo_form on the workspace default must NOT push the "
        "token-collecting form"
    )
    texts = _ephemeral_texts(client_fake)
    assert len(texts) == 1, f"exactly one refusal ephemeral must be posted, got {texts!r}"
    assert "workspace-admin" in texts[0], (
        f"the refusal must name the permission the caller lacks, got {texts[0]!r}"
    )


@pytest.mark.asyncio
async def test_handle_agent_setup_action_edit_repo_form_admin_on_default_agent_pushes_form(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: object,
) -> None:
    """An admin still gets the edit-repo form for the workspace's default agent.

    Binding a repo to the agent the whole workspace uses is the ordinary
    first-run step; the gate refuses non-admins, not admins.
    """
    tenant_id, fernet_key, _ = await _seed_team(db_session)
    agent_payload = _agent_payload(tenant_id=tenant_id)
    handler = _make_ma_handler_with_agents([agent_payload])
    runtime = _build_runtime(fernet_key, db_session_factory, anthropic_handler=handler)
    await _mark_reachable(db_session_factory, tenant_id=tenant_id, agent_name=_AGENT_NAME)

    client_fake: Any = fake_slack_web_client
    _override_users_info_admin(client_fake.mock)

    payload = _action_payload(
        "agent_setup__edit_repo_form", selected_agent_name=_AGENT_NAME, active_section="repo_auth"
    )
    await handle_agent_setup_action(runtime, payload)  # type: ignore[arg-type]

    assert _get_push_callback_id(client_fake) == "agent_setup__edit_repo", (
        "admin agent_setup__edit_repo_form on the workspace default must still push the form"
    )
    assert not _ephemeral_texts(client_fake), (
        "an admin must not be refused on the workspace default agent"
    )


@pytest.mark.asyncio
async def test_handle_agent_setup_action_paste_secrets_non_admin_pushes_form_no_ephemeral(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: object,
) -> None:
    """The paste-secrets form is pushed for every member, unlike the edit-repo form.

    It prompts for nothing at push time, so nothing is at risk in showing it;
    the write it submits is refused at submission when the target is shared. If
    this test starts failing, a gate was added where none belongs -- fix the
    handler, not the test.
    """
    tenant_id, fernet_key, _ = await _seed_team(db_session)
    agent_payload = _agent_payload(tenant_id=tenant_id)
    handler = _make_ma_handler_with_agents([agent_payload])
    runtime = _build_runtime(fernet_key, db_session_factory, anthropic_handler=handler)
    # Shared target on purpose: this is the case a push-time gate would refuse.
    await _mark_reachable(db_session_factory, tenant_id=tenant_id, agent_name=_AGENT_NAME)

    payload = _action_payload(
        "agent_setup__paste_secrets", selected_agent_name=_AGENT_NAME, active_section="secrets"
    )
    await handle_agent_setup_action(runtime, payload)  # type: ignore[arg-type]

    client_fake: Any = fake_slack_web_client
    assert _get_push_callback_id(client_fake) == "agent_setup__paste_secrets", (
        "non-admin agent_setup__paste_secrets must push the paste-secrets form"
    )
    _assert_no_refusal(client_fake)


@pytest.mark.asyncio
async def test_handle_agent_setup_action_remove_secret_non_admin_on_default_agent_refuses_and_writes_nothing(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: object,
) -> None:
    """A non-admin cannot delete an env variable from the workspace's default agent.

    Removing a variable the shared agent's integrations depend on is
    destructive and silent -- the next turn simply loses it -- so the gate has
    to fire before the delete, not after.
    """
    tenant_id, fernet_key, _ = await _seed_team(db_session)
    agent_payload = _agent_payload(tenant_id=tenant_id)
    handler = _make_ma_handler_with_agents([agent_payload])
    runtime = _build_runtime(fernet_key, db_session_factory, anthropic_handler=handler)
    await _mark_reachable(db_session_factory, tenant_id=tenant_id, agent_name=_AGENT_NAME)

    agent_uuid = derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=_MA_AGENT_ID)
    async with db_session_factory() as session, session.begin():
        await put_agent_file(
            session,
            tenant_id=tenant_id,
            agent_id=agent_uuid,
            key="SOME_KEY",
            content="some-value",
        )

    payload = _action_payload(
        "agent_setup__remove_secret",
        selected_agent_name=_AGENT_NAME,
        active_section="secrets",
        action_extra={"selected_option": {"value": "SOME_KEY"}},
    )
    await handle_agent_setup_action(runtime, payload)  # type: ignore[arg-type]

    async with db_session_factory() as session:
        surviving = await get_agent_file(
            session, tenant_id=tenant_id, agent_id=agent_uuid, key="SOME_KEY"
        )
    assert surviving is not None, (
        "non-admin remove_secret on the workspace default must delete no agent file"
    )

    client_fake: Any = fake_slack_web_client
    texts = _ephemeral_texts(client_fake)
    assert len(texts) == 1, f"exactly one refusal ephemeral must be posted, got {texts!r}"
    assert "workspace-admin" in texts[0], (
        f"the refusal must name the permission the caller lacks, got {texts[0]!r}"
    )
    assert "SOME_KEY" not in texts[0], (
        f"the refusal must not echo the secret key back to the channel, got {texts[0]!r}"
    )


@pytest.mark.asyncio
async def test_handle_agent_setup_action_remove_secret_admin_on_default_agent_deletes_the_file(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: object,
) -> None:
    """An admin still removes env variables from the workspace's default agent."""
    tenant_id, fernet_key, _ = await _seed_team(db_session)
    agent_payload = _agent_payload(tenant_id=tenant_id)
    handler = _make_ma_handler_with_agents([agent_payload])
    runtime = _build_runtime(fernet_key, db_session_factory, anthropic_handler=handler)
    await _mark_reachable(db_session_factory, tenant_id=tenant_id, agent_name=_AGENT_NAME)

    agent_uuid = derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=_MA_AGENT_ID)
    async with db_session_factory() as session, session.begin():
        await put_agent_file(
            session,
            tenant_id=tenant_id,
            agent_id=agent_uuid,
            key="SOME_KEY",
            content="some-value",
        )

    client_fake: Any = fake_slack_web_client
    _override_users_info_admin(client_fake.mock)

    payload = _action_payload(
        "agent_setup__remove_secret",
        selected_agent_name=_AGENT_NAME,
        active_section="secrets",
        action_extra={"selected_option": {"value": "SOME_KEY"}},
    )
    await handle_agent_setup_action(runtime, payload)  # type: ignore[arg-type]

    async with db_session_factory() as session:
        removed = await get_agent_file(
            session, tenant_id=tenant_id, agent_id=agent_uuid, key="SOME_KEY"
        )
    assert removed is None, (
        "admin remove_secret on the workspace default must delete the agent file"
    )
    assert _has_update(client_fake), "admin remove_secret must re-render L2 after the delete"


# ---------------------------------------------------------------------------
# Admin-only-unconditional branches: delete, scope:channel, scope:clear,
# connect_mcp -- unchanged behavior, refused for a non-admin regardless of
# reachability. scope:workspace is already covered above.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_agent_setup_action_delete_non_admin_refused_no_write(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: object,
) -> None:
    tenant_id, fernet_key, _ = await _seed_team(db_session)
    agent_payload = _agent_payload(tenant_id=tenant_id)
    handler = _make_ma_handler_with_agents([agent_payload])
    runtime = _build_runtime(fernet_key, db_session_factory, anthropic_handler=handler)

    payload = _action_payload("agent_setup__delete", selected_agent_name=_AGENT_NAME)
    await handle_agent_setup_action(runtime, payload)  # type: ignore[arg-type]

    client_fake: Any = fake_slack_web_client
    texts = _ephemeral_texts(client_fake)
    assert any("no longer have permission" in t for t in texts), (
        "non-admin agent_setup__delete must be refused (admin-only, unconditional)"
    )


# ---------------------------------------------------------------------------
# Delete against the deployment's built-in agent.
#
# ``write.delete_agent`` raises for this case and stays the server-side
# backstop, but on Slack a raise reaches only the boundary catch at the bottom
# of handle_agent_setup_action -- log + Sentry, no Slack call -- and the caller
# is fire-and-forget, so the click renders as nothing happening at all. These
# two tests pin the visible refusal and its unmanaged counterpart.
# ---------------------------------------------------------------------------


def _make_ma_handler_recording_archive(
    agent_payloads: list[dict[str, object]],
    recorded_requests: list[str],
) -> Any:
    """Serve the given agents, record every request, and honour archive.

    Extends the read-only handler above with POST ``/v1/agents/{id}/archive``
    so a delete that is NOT refused runs to completion (archive, then the
    roster re-read that repaints L1). ``recorded_requests`` collects
    ``"METHOD /path"`` for every call, which is what lets a test assert that no
    archive was ever attempted.
    """
    agent_store: dict[str, dict[str, object]] = {str(ag["id"]): ag for ag in agent_payloads}
    archived_ids: set[str] = set()

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        method = request.method
        recorded_requests.append(f"{method} {path}")

        if method == "GET" and path == "/v1/agents":
            live = [ag for agent_id, ag in agent_store.items() if agent_id not in archived_ids]
            return httpx.Response(200, json={"data": live, "has_more": False})

        archive_match = re.match(r"^/v1/agents/(?P<id>[^/]+)/archive$", path)
        if archive_match and method == "POST":
            agent_id_req = archive_match.group("id")
            if agent_id_req not in agent_store:
                return httpx.Response(
                    404,
                    json={
                        "type": "error",
                        "error": {"type": "not_found_error", "message": "not found"},
                    },
                )
            archived_ids.add(agent_id_req)
            return httpx.Response(
                200,
                json={
                    **agent_store[agent_id_req],
                    "archived_at": datetime.now(UTC).isoformat(),
                },
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

        if method == "GET" and path == "/v1/environments":
            return httpx.Response(200, json={"data": [], "has_more": False})

        return httpx.Response(404, json={"error": f"unhandled {method} {path}"})

    return handler


def _views_update_bodies(client_fake: Any) -> list[str]:
    """Serialized bodies of every views.update call, for hint assertions."""
    calls = client_fake.mock.requests.get(("POST", yarl.URL(f"{_SLACK_API_BASE}/views.update")), [])
    return [json.dumps(call.kwargs.get("json") or {}) for call in calls]


@pytest.mark.asyncio
async def test_handle_agent_setup_action_delete_builtin_agent_refuses_visibly_and_archives_nothing(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: object,
) -> None:
    """Delete on the deployment's built-in agent refuses in Slack, not just server-side.

    The roster carries no managed flag, so the panel offers Delete on a seeded
    agent exactly as it does on a user agent. An admin clicking it must see why
    it will not happen -- a silent no-op invites a second click and turns an
    expected policy refusal into a Sentry event.
    """
    tenant_id, fernet_key, _ = await _seed_team(db_session)

    now = datetime.now(UTC)
    builtin_agent = BetaManagedAgentsAgent(
        id=_MA_AGENT_ID,
        type="agent",
        name=_AGENT_NAME,
        version=1,
        model=BetaManagedAgentsModelConfig(id="claude-sonnet-4-6", speed="standard"),
        metadata={
            MA_METADATA_KEY_TENANT: str(tenant_id),
            MA_METADATA_KEY_NAME: _AGENT_NAME,
            MA_METADATA_KEY_MANAGED: "true",
        },
        mcp_servers=[],
        skills=[],
        tools=[],
        created_at=now,
        updated_at=now,
    ).model_dump(mode="json")

    recorded_requests: list[str] = []
    handler = _make_ma_handler_recording_archive([builtin_agent], recorded_requests)
    runtime = _build_runtime(fernet_key, db_session_factory, anthropic_handler=handler)

    client_fake: Any = fake_slack_web_client
    _override_users_info_admin(client_fake.mock)

    payload = _action_payload("agent_setup__delete", selected_agent_name=_AGENT_NAME)
    await handle_agent_setup_action(runtime, payload)  # type: ignore[arg-type]

    assert not [req for req in recorded_requests if req.endswith("/archive")], (
        "deleting the built-in agent must reach no archive call on the Anthropic "
        f"transport, got requests: {recorded_requests!r}"
    )

    texts = _ephemeral_texts(client_fake)
    assert len(texts) == 1, f"exactly one refusal ephemeral must be posted, got {texts!r}"
    assert "built-in agent" in texts[0], (
        f"the refusal must say the agent is built in, got {texts[0]!r}"
    )
    assert "ork" in texts[0], (
        f"the refusal must point at forking as the way forward, got {texts[0]!r}"
    )

    update_bodies = _views_update_bodies(client_fake)
    assert not update_bodies, (
        "a refused delete must leave the open panel untouched -- the agent still "
        f"exists, so the rendered view is still accurate; got {update_bodies!r}"
    )
    assert not any("wastebasket" in body or "deleted." in body for body in update_bodies), (
        "the panel must never claim the built-in agent was deleted, got "
        f"views.update bodies: {update_bodies!r}"
    )


@pytest.mark.asyncio
async def test_handle_agent_setup_action_delete_unmanaged_agent_archives_and_repaints(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: object,
) -> None:
    """An ordinary user agent still deletes -- the built-in refusal must not over-block."""
    tenant_id, fernet_key, _ = await _seed_team(db_session)

    now = datetime.now(UTC)
    user_agent = BetaManagedAgentsAgent(
        id=_MA_AGENT_ID,
        type="agent",
        name=_AGENT_NAME,
        version=1,
        model=BetaManagedAgentsModelConfig(id="claude-sonnet-4-6", speed="standard"),
        metadata={
            MA_METADATA_KEY_TENANT: str(tenant_id),
            MA_METADATA_KEY_NAME: _AGENT_NAME,
        },
        mcp_servers=[],
        skills=[],
        tools=[],
        created_at=now,
        updated_at=now,
    ).model_dump(mode="json")

    # A second agent survives the delete: the L1 empty state replaces the hint
    # line entirely, so the repaint is only observable on a non-empty roster.
    surviving_agent = BetaManagedAgentsAgent(
        id=f"agent_{'b' * 24}",
        type="agent",
        name="other-agent",
        version=1,
        model=BetaManagedAgentsModelConfig(id="claude-sonnet-4-6", speed="standard"),
        metadata={
            MA_METADATA_KEY_TENANT: str(tenant_id),
            MA_METADATA_KEY_NAME: "other-agent",
        },
        mcp_servers=[],
        skills=[],
        tools=[],
        created_at=now,
        updated_at=now,
    ).model_dump(mode="json")

    recorded_requests: list[str] = []
    handler = _make_ma_handler_recording_archive([user_agent, surviving_agent], recorded_requests)
    runtime = _build_runtime(fernet_key, db_session_factory, anthropic_handler=handler)

    client_fake: Any = fake_slack_web_client
    _override_users_info_admin(client_fake.mock)

    payload = _action_payload("agent_setup__delete", selected_agent_name=_AGENT_NAME)
    await handle_agent_setup_action(runtime, payload)  # type: ignore[arg-type]

    assert f"POST /v1/agents/{_MA_AGENT_ID}/archive" in recorded_requests, (
        "deleting an unmanaged agent must archive it on the Anthropic transport, got "
        f"requests: {recorded_requests!r}"
    )
    assert not _ephemeral_texts(client_fake), (
        f"an allowed delete must post no refusal ephemeral, got {_ephemeral_texts(client_fake)!r}"
    )

    update_bodies = _views_update_bodies(client_fake)
    assert len(update_bodies) == 1, (
        f"a successful delete must repaint L1 exactly once, got {update_bodies!r}"
    )
    # The repaint is asserted through the roster it renders, not through the
    # delete hint: build_l1_view drops scope_hint whenever no agent is
    # selected, and the success path deliberately clears the selection, so
    # ``delete_hint`` never reaches the rendered view.
    assert _AGENT_NAME not in update_bodies[0], (
        f"the repainted roster must no longer offer the archived agent, got {update_bodies[0]!r}"
    )
    assert "other-agent" in update_bodies[0], (
        f"the repainted roster must still offer the surviving agent, got {update_bodies[0]!r}"
    )


@pytest.mark.asyncio
async def test_handle_agent_setup_action_scope_channel_non_admin_refused_no_write(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: object,
) -> None:
    tenant_id, fernet_key, _ = await _seed_team(db_session)
    runtime = _build_runtime(fernet_key, db_session_factory)

    payload = _action_payload(
        "agent_setup__scope:channel",
        selected_agent_name=_AGENT_NAME,
        action_extra={"selected_channel": _CHANNEL_ID},
    )
    await handle_agent_setup_action(runtime, payload)  # type: ignore[arg-type]

    async with db_session_factory() as session:
        row = await get_scope(
            session, scope=ChannelScopeRef(tenant_id=tenant_id, channel_id=_CHANNEL_ID)
        )
    assert row is None, "non-admin scope:channel must not write a DB row (fail-closed)"

    client_fake: Any = fake_slack_web_client
    texts = _ephemeral_texts(client_fake)
    assert any("no longer have permission" in t for t in texts), (
        "non-admin agent_setup__scope:channel must be refused (admin-only, unconditional)"
    )


@pytest.mark.asyncio
async def test_handle_agent_setup_action_scope_clear_non_admin_refused_no_write(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: object,
) -> None:
    tenant_id, fernet_key, _ = await _seed_team(db_session)
    runtime = _build_runtime(fernet_key, db_session_factory)
    await _mark_reachable(db_session_factory, tenant_id=tenant_id, agent_name=_AGENT_NAME)

    payload = _action_payload("agent_setup__scope:clear", selected_agent_name=_AGENT_NAME)
    await handle_agent_setup_action(runtime, payload)  # type: ignore[arg-type]

    async with db_session_factory() as session:
        row = await get_scope(session, scope=TenantScopeRef(tenant_id=tenant_id))
    assert row is not None and row.agent_name == _AGENT_NAME, (
        "non-admin scope:clear must NOT clear the existing scope row (fail-closed)"
    )

    client_fake: Any = fake_slack_web_client
    texts = _ephemeral_texts(client_fake)
    assert any("no longer have permission" in t for t in texts), (
        "non-admin agent_setup__scope:clear must be refused (admin-only, unconditional)"
    )


@pytest.mark.asyncio
async def test_handle_agent_setup_action_connect_mcp_non_admin_refused_no_ephemeral_config(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: object,
) -> None:
    """connect_mcp mints a bearer token and stays admin-only unconditionally (fact 5)."""
    tenant_id, fernet_key, _ = await _seed_team(db_session)
    agent_payload = _agent_payload(tenant_id=tenant_id)
    handler = _make_ma_handler_with_agents([agent_payload])

    settings = MagicMock()
    settings.crypto.keys = (SecretStr(fernet_key),)
    settings.mcp.public_url = "https://mcp.example.com"
    settings.mcp.jwt_secret = SecretStr("test-secret-32-bytes-long-padding!")
    settings.github = MagicMock()
    settings.github.app_id = None
    runtime = SlackRuntime(
        settings=settings,
        anthropic=build_fake_anthropic(handler),
        sessionmaker=db_session_factory,
        billing_config=None,
        http_client=MagicMock(spec=httpx.AsyncClient),
        resolver_cache=MagicMock(),  # pyright: ignore[reportArgumentType]  # stub, turn path not exercised
        turn_deps=MagicMock(),  # pyright: ignore[reportArgumentType]  # stub, turn path not exercised
    )

    payload = _action_payload("agent_setup__connect_mcp", selected_agent_name=_AGENT_NAME)
    await handle_agent_setup_action(runtime, payload)  # type: ignore[arg-type]

    client_fake: Any = fake_slack_web_client
    texts = _ephemeral_texts(client_fake)
    assert any("no longer have permission" in t for t in texts), (
        "non-admin agent_setup__connect_mcp must be refused (admin-only, unconditional)"
    )
    assert not any(":link: *Connect via MCP" in t for t in texts), (
        "non-admin agent_setup__connect_mcp must NOT receive the MCP config ephemeral"
    )


# ---------------------------------------------------------------------------
# Field-conditional branches, gated by
# refuse_if_reachable_and_not_admin: remove_skill, remove_mcp,
# edit_agent_form, add_skill, add_mcp. Each proceeds for a non-admin when the
# target agent is unreachable, and is refused when it is currently reachable
# -- proved against a real propagation row (scoped_config_write.set_fields),
# never by patching the predicate.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_agent_setup_action_remove_skill_non_admin_unreachable_proceeds(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: object,
) -> None:
    tenant_id, fernet_key, _ = await _seed_team(db_session)
    agent_payload = _agent_payload(tenant_id=tenant_id)
    agent_payload["skills"] = [{"type": "anthropic", "skill_id": "cli-auth"}]
    handler = _make_ma_handler_with_agents_and_update([agent_payload])
    runtime = _build_runtime(fernet_key, db_session_factory, anthropic_handler=handler)

    payload = _action_payload(
        "agent_setup__remove_skill",
        selected_agent_name=_AGENT_NAME,
        active_section="skills",
        action_extra={"selected_option": {"value": "cli-auth"}},
    )
    await handle_agent_setup_action(runtime, payload)  # type: ignore[arg-type]

    client_fake: Any = fake_slack_web_client
    _assert_no_refusal(client_fake)
    assert _has_update(client_fake), (
        "non-admin remove_skill on an unreachable agent must proceed and re-render L2"
    )


@pytest.mark.asyncio
async def test_handle_agent_setup_action_remove_skill_non_admin_reachable_refused(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: object,
) -> None:
    tenant_id, fernet_key, _ = await _seed_team(db_session)
    runtime = _build_runtime(fernet_key, db_session_factory)
    await _mark_reachable(db_session_factory, tenant_id=tenant_id, agent_name=_AGENT_NAME)

    payload = _action_payload(
        "agent_setup__remove_skill",
        selected_agent_name=_AGENT_NAME,
        active_section="skills",
        action_extra={"selected_option": {"value": "cli-auth"}},
    )
    await handle_agent_setup_action(runtime, payload)  # type: ignore[arg-type]

    client_fake: Any = fake_slack_web_client
    _assert_reachability_refusal(client_fake)
    assert not _has_update(client_fake), (
        "non-admin remove_skill on a reachable agent must be refused before any re-render"
    )


@pytest.mark.asyncio
async def test_handle_agent_setup_action_remove_mcp_non_admin_unreachable_proceeds(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: object,
) -> None:
    tenant_id, fernet_key, _ = await _seed_team(db_session)
    agent_payload = _agent_payload(tenant_id=tenant_id)
    agent_payload["mcp_servers"] = [{"name": "an-mcp", "url": "https://mcp.example.com"}]
    handler = _make_ma_handler_with_agents_and_update([agent_payload])
    runtime = _build_runtime(fernet_key, db_session_factory, anthropic_handler=handler)

    payload = _action_payload(
        "agent_setup__remove_mcp",
        selected_agent_name=_AGENT_NAME,
        active_section="mcps",
        action_extra={"selected_option": {"value": "an-mcp"}},
    )
    await handle_agent_setup_action(runtime, payload)  # type: ignore[arg-type]

    client_fake: Any = fake_slack_web_client
    _assert_no_refusal(client_fake)
    assert _has_update(client_fake), (
        "non-admin remove_mcp on an unreachable agent must proceed and re-render L2"
    )


@pytest.mark.asyncio
async def test_handle_agent_setup_action_remove_mcp_non_admin_reachable_refused(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: object,
) -> None:
    tenant_id, fernet_key, _ = await _seed_team(db_session)
    runtime = _build_runtime(fernet_key, db_session_factory)
    await _mark_reachable(db_session_factory, tenant_id=tenant_id, agent_name=_AGENT_NAME)

    payload = _action_payload(
        "agent_setup__remove_mcp",
        selected_agent_name=_AGENT_NAME,
        active_section="mcps",
        action_extra={"selected_option": {"value": "an-mcp"}},
    )
    await handle_agent_setup_action(runtime, payload)  # type: ignore[arg-type]

    client_fake: Any = fake_slack_web_client
    _assert_reachability_refusal(client_fake)
    assert not _has_update(client_fake), (
        "non-admin remove_mcp on a reachable agent must be refused before any re-render"
    )


@pytest.mark.asyncio
async def test_handle_agent_setup_action_edit_agent_form_non_admin_unreachable_proceeds(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: object,
) -> None:
    tenant_id, fernet_key, _ = await _seed_team(db_session)
    agent_payload = _agent_payload(tenant_id=tenant_id)
    handler = _make_ma_handler_with_agents([agent_payload])
    runtime = _build_runtime(fernet_key, db_session_factory, anthropic_handler=handler)

    payload = _action_payload(
        "agent_setup__edit_agent_form", selected_agent_name=_AGENT_NAME, active_section="agent"
    )
    await handle_agent_setup_action(runtime, payload)  # type: ignore[arg-type]

    client_fake: Any = fake_slack_web_client
    assert _get_push_callback_id(client_fake) == "agent_setup__edit_agent", (
        "non-admin edit_agent_form on an unreachable agent must push the edit-agent form"
    )
    _assert_no_refusal(client_fake)


@pytest.mark.asyncio
async def test_handle_agent_setup_action_edit_agent_form_non_admin_reachable_refused(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: object,
) -> None:
    tenant_id, fernet_key, _ = await _seed_team(db_session)
    runtime = _build_runtime(fernet_key, db_session_factory)
    await _mark_reachable(db_session_factory, tenant_id=tenant_id, agent_name=_AGENT_NAME)

    payload = _action_payload(
        "agent_setup__edit_agent_form", selected_agent_name=_AGENT_NAME, active_section="agent"
    )
    await handle_agent_setup_action(runtime, payload)  # type: ignore[arg-type]

    client_fake: Any = fake_slack_web_client
    _assert_reachability_refusal(client_fake)
    assert not _has_push(client_fake), (
        "non-admin edit_agent_form on a reachable agent must be refused before any push"
    )


@pytest.mark.asyncio
async def test_handle_agent_setup_action_add_skill_non_admin_unreachable_proceeds(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: object,
) -> None:
    tenant_id, fernet_key, _ = await _seed_team(db_session)
    agent_payload = _agent_payload(tenant_id=tenant_id)
    handler = _make_ma_handler_with_agents([agent_payload])
    runtime = _build_runtime(fernet_key, db_session_factory, anthropic_handler=handler)

    payload = _action_payload(
        "agent_setup__add_skill", selected_agent_name=_AGENT_NAME, active_section="skills"
    )
    await handle_agent_setup_action(runtime, payload)  # type: ignore[arg-type]

    client_fake: Any = fake_slack_web_client
    assert _get_push_callback_id(client_fake) == "agent_setup__add_skill", (
        "non-admin add_skill on an unreachable agent must push the add-skill form"
    )
    _assert_no_refusal(client_fake)


@pytest.mark.asyncio
async def test_handle_agent_setup_action_add_skill_non_admin_reachable_refused(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: object,
) -> None:
    tenant_id, fernet_key, _ = await _seed_team(db_session)
    runtime = _build_runtime(fernet_key, db_session_factory)
    await _mark_reachable(db_session_factory, tenant_id=tenant_id, agent_name=_AGENT_NAME)

    payload = _action_payload(
        "agent_setup__add_skill", selected_agent_name=_AGENT_NAME, active_section="skills"
    )
    await handle_agent_setup_action(runtime, payload)  # type: ignore[arg-type]

    client_fake: Any = fake_slack_web_client
    _assert_reachability_refusal(client_fake)
    assert not _has_push(client_fake), (
        "non-admin add_skill on a reachable agent must be refused before any push"
    )


@pytest.mark.asyncio
async def test_handle_agent_setup_action_add_mcp_non_admin_unreachable_proceeds(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: object,
) -> None:
    tenant_id, fernet_key, _ = await _seed_team(db_session)
    agent_payload = _agent_payload(tenant_id=tenant_id)
    handler = _make_ma_handler_with_agents([agent_payload])
    runtime = _build_runtime(fernet_key, db_session_factory, anthropic_handler=handler)

    payload = _action_payload(
        "agent_setup__add_mcp", selected_agent_name=_AGENT_NAME, active_section="mcps"
    )
    await handle_agent_setup_action(runtime, payload)  # type: ignore[arg-type]

    client_fake: Any = fake_slack_web_client
    assert _get_push_callback_id(client_fake) == "agent_setup__add_mcp", (
        "non-admin add_mcp on an unreachable agent must push the add-mcp form"
    )
    _assert_no_refusal(client_fake)


@pytest.mark.asyncio
async def test_handle_agent_setup_action_add_mcp_non_admin_reachable_refused(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: object,
) -> None:
    tenant_id, fernet_key, _ = await _seed_team(db_session)
    runtime = _build_runtime(fernet_key, db_session_factory)
    await _mark_reachable(db_session_factory, tenant_id=tenant_id, agent_name=_AGENT_NAME)

    payload = _action_payload(
        "agent_setup__add_mcp", selected_agent_name=_AGENT_NAME, active_section="mcps"
    )
    await handle_agent_setup_action(runtime, payload)  # type: ignore[arg-type]

    client_fake: Any = fake_slack_web_client
    _assert_reachability_refusal(client_fake)
    assert not _has_push(client_fake), (
        "non-admin add_mcp on a reachable agent must be refused before any push"
    )


@pytest.mark.asyncio
async def test_handle_agent_setup_action_add_mcp_admin_on_reachable_agent_unaffected(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: object,
) -> None:
    """An admin caller is unaffected by reachability on a field-conditional branch."""
    tenant_id, fernet_key, _ = await _seed_team(db_session)
    agent_payload = _agent_payload(tenant_id=tenant_id)
    handler = _make_ma_handler_with_agents([agent_payload])
    runtime = _build_runtime(fernet_key, db_session_factory, anthropic_handler=handler)
    await _mark_reachable(db_session_factory, tenant_id=tenant_id, agent_name=_AGENT_NAME)

    client_fake: Any = fake_slack_web_client
    _override_users_info_admin(client_fake.mock)

    payload = _action_payload(
        "agent_setup__add_mcp", selected_agent_name=_AGENT_NAME, active_section="mcps"
    )
    await handle_agent_setup_action(runtime, payload)  # type: ignore[arg-type]

    assert _get_push_callback_id(client_fake) == "agent_setup__add_mcp", (
        "admin add_mcp on a reachable agent must still push the add-mcp form"
    )
    _assert_no_refusal(client_fake)
