"""Tests for the read-only setup panel's slash entry and block actions.

Covers the navigation contract the three views depend on: paging updates the
view it was clicked on and never pushes, Details and Who-answers-where push
exactly once and carry their own metadata, the Details expansions update in
place, a failed read renders an error view rather than leaving a stale render
on screen, and the setup button keeps the agent it was clicked beside.

Real Postgres (`db_session_factory`), the transport-level Anthropic fake, and
the `aioresponses`-backed `AsyncWebClient` from conftest — no method-level
mocks on the Slack client.
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
import yarl
from aioresponses import aioresponses as AioResponsesMock
from cryptography.fernet import Fernet
from daimon.adapters.slack.agent_setup.actions import (
    CHANNEL_ADMINS_NEED_ADMIN_MESSAGE,
    handle_agent_setup_action,
    handle_agent_setup_command,
)
from daimon.adapters.slack.agent_setup.isolation import ISOLATION_NEED_ADMIN_MESSAGE
from daimon.adapters.slack.agent_setup.panel_views import (
    ACTION_ADD_SKILL,
    ACTION_CHANNEL_ADMINS,
    ACTION_CODING_TOOLS,
    ACTION_DETAILS,
    ACTION_EXPAND_CONNECTIONS,
    ACTION_EXPAND_KEYS,
    ACTION_ISOLATE,
    ACTION_ISOLATE_COPY,
    ACTION_LIFT_ISOLATION,
    ACTION_NEW,
    ACTION_PAGE_NEXT,
    ACTION_REVOKE_TOKEN,
    ACTION_ROUTING,
    CALLBACK_ADD_SKILL,
)
from daimon.adapters.slack.agent_setup.state import (
    PanelMetadata,
    decode_panel_metadata,
    encode_panel_metadata,
)
from daimon.adapters.slack.runtime import SlackRuntime
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.agent_pins import PIN_WRITE_REFUSAL
from daimon.core.github_credentials import build_multifernet, encrypt_token
from daimon.core.scope import ChannelScopeRef, DeploymentDefault, TenantScopeRef
from daimon.core.stores.access_policy import load_access_policy, set_access_policy
from daimon.core.stores.identity import get_or_create_platform_principal
from daimon.core.stores.scoped_config_write import set_fields
from daimon.core.stores.slack_bot_tokens import upsert_slack_bot_token
from daimon.core.stores.thread_agent_bindings import get_binding
from daimon.core.stores.thread_sessions import create_thread_session
from daimon.testing.factories import make_tenant
from daimon.testing.ma import (
    FakeMAState,
    NotHandled,
    build_fake_anthropic,
    combine_handlers,
    make_fake_ma_handler,
)
from daimon.testing.ma_models import ma_agent
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_TEAM_ID = "T_PANEL_TESTS"
_USER_ID = "U_PANEL_TEST"
_CHANNEL_ID = "C_PANEL_TEST"
_ROOT_VIEW_ID = "V_PANEL_ROOT"
_VIEW_HASH = "H_PANEL_ROOT"
_ANSWERING_AGENT = "answering-agent"
_OTHER_AGENT = "aardvark-agent"
_ROUTED_ELSEWHERE_AGENT = "elsewhere-agent"

_SLACK_API_BASE = "https://slack.com/api"
_VIEWS_UPDATE_KEY = ("POST", yarl.URL(f"{_SLACK_API_BASE}/views.update"))
_VIEWS_PUSH_KEY = ("POST", yarl.URL(f"{_SLACK_API_BASE}/views.push"))
_VIEWS_OPEN_KEY = ("POST", yarl.URL(f"{_SLACK_API_BASE}/views.open"))


# ---------------------------------------------------------------------------
# Seeding and fakes
# ---------------------------------------------------------------------------


async def _seed_team(session: AsyncSession) -> tuple[uuid.UUID, str]:
    """Tenant + encrypted bot token. Returns (tenant_id, fernet_key)."""
    fernet_key = Fernet.generate_key().decode()
    encrypted = encrypt_token(build_multifernet((fernet_key,)), "xoxb-test")
    tenant = await make_tenant(session, platform="slack", workspace_id=_TEAM_ID)
    await upsert_slack_bot_token(session, team_id=_TEAM_ID, encrypted_token=encrypted)
    await session.flush()
    return tenant.id, fernet_key


def _agent_payload(*, tenant_id: uuid.UUID, name: str, agent_id: str) -> dict[str, Any]:
    now = datetime.now(UTC).isoformat()
    return {
        "id": agent_id,
        "type": "agent",
        "name": name,
        "version": 1,
        "model": {"id": "claude-sonnet-4-6", "speed": "standard"},
        "system": f"{name} helps with things.",
        "metadata": {"daimon_tenant": str(tenant_id), "daimon_name": name},
        "mcp_servers": [],
        "tools": [],
        "skills": [],
        "created_at": now,
        "updated_at": now,
        "archived_at": None,
        "description": None,
    }


def _ma_handler(agents: list[dict[str, Any]], *, fail_list: bool = False) -> Any:
    """Serve the tenant's agents, or a hard 404 on the listing."""
    store = {str(agent["id"]): agent for agent in agents}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path == "/v1/agents":
            if fail_list:
                return httpx.Response(
                    404,
                    json={
                        "type": "error",
                        "error": {"type": "not_found_error", "message": "gone"},
                    },
                )
            return httpx.Response(200, json={"data": list(store.values()), "has_more": False})
        if request.method == "GET" and path.startswith("/v1/agents/"):
            agent = store.get(path.removeprefix("/v1/agents/"))
            if agent is not None:
                return httpx.Response(200, json=agent)
            return httpx.Response(
                404,
                json={"type": "error", "error": {"type": "not_found_error", "message": "gone"}},
            )
        if request.method == "GET" and path == "/v1/environments":
            return httpx.Response(200, json={"data": [], "has_more": False})
        return httpx.Response(404, json={"error": f"unhandled {request.method} {path}"})

    return handler


def _build_runtime(
    fernet_key: str,
    db_factory: async_sessionmaker[AsyncSession],
    *,
    handler: Any,
    deployment_default: DeploymentDefault | None = None,
) -> SlackRuntime:
    settings = MagicMock()
    settings.crypto.keys = (SecretStr(fernet_key),)
    settings.mcp.public_url = None
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
        deployment_default=deployment_default or DeploymentDefault(),
    )


def _meta(**overrides: Any) -> PanelMetadata:
    base: dict[str, Any] = {
        "team_id": _TEAM_ID,
        "channel_id": _CHANNEL_ID,
        "view": "agents",
    }
    base.update(overrides)
    return PanelMetadata(**base)


def _action_payload(
    action_id: str,
    *,
    meta: PanelMetadata,
    value: str | None = None,
    view_id: str = _ROOT_VIEW_ID,
    view_hash: str = _VIEW_HASH,
) -> dict[str, Any]:
    action: dict[str, Any] = {"action_id": action_id}
    if value is not None:
        action["value"] = value
    return {
        "team": {"id": _TEAM_ID},
        "user": {"id": _USER_ID},
        "trigger_id": "TRIG_PANEL",
        "response_url": "https://hooks.slack.test/actions/response",
        "actions": [action],
        "view": {
            "id": view_id,
            "hash": view_hash,
            "private_metadata": encode_panel_metadata(meta),
        },
    }


def _roster_row(blocks: list[dict[str, Any]], agent_name: str) -> str:
    """The single roster row carrying `agent_name` and its routing status."""
    for block in blocks:
        text = str((block.get("text") or {}).get("text", ""))
        if block.get("type") == "section" and f"*{agent_name}*" in text:
            return text
    raise AssertionError(f"no roster row for {agent_name}")


def _sent(mock: AioResponsesMock, key: tuple[str, yarl.URL]) -> list[dict[str, Any]]:
    return [dict(kwargs.get("json") or {}) for _, kwargs in mock.requests.get(key, [])]


def _override(mock: AioResponsesMock, method: str, payload: dict[str, Any]) -> None:
    """Replace conftest's canned response for one Slack method.

    aioresponses matches in insertion order and the conftest entries are
    registered with repeat=True, so a plain re-register would never be reached.
    """
    url = f"{_SLACK_API_BASE}/{method}"
    for key in [
        key
        for key, matcher in mock._matches.items()  # type: ignore[attr-defined]
        if str(getattr(matcher, "url_or_pattern", "")) == url
    ]:
        del mock._matches[key]  # type: ignore[attr-defined]
    mock.post(url, payload=payload, repeat=True)  # pyright: ignore[reportUnknownMemberType]


async def _mark_tenant_default(
    db_factory: async_sessionmaker[AsyncSession], *, tenant_id: uuid.UUID, agent_name: str
) -> None:
    """Make `agent_name` the workspace default with a real propagation row."""
    async with db_factory() as session, session.begin():
        await set_fields(
            session,
            scope=TenantScopeRef(tenant_id=tenant_id),
            tenant_id=tenant_id,
            agent_name=agent_name,
            mode="agent",
        )


# ---------------------------------------------------------------------------
# Slash entry
# ---------------------------------------------------------------------------


async def test_command_renders_agents_view_with_answering_agent_first(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
) -> None:
    """The agent answering where the caller stands leads the list.

    Name order would put `aardvark-agent` first; the responder for this
    channel is what the caller is asking about, so it goes above it.
    """
    tenant_id, fernet_key = await _seed_team(db_session)
    await db_session.commit()
    await _mark_tenant_default(db_session_factory, tenant_id=tenant_id, agent_name=_ANSWERING_AGENT)

    runtime = _build_runtime(
        fernet_key,
        db_session_factory,
        handler=_ma_handler(
            [
                _agent_payload(tenant_id=tenant_id, name=_OTHER_AGENT, agent_id="agent_other"),
                _agent_payload(
                    tenant_id=tenant_id, name=_ANSWERING_AGENT, agent_id="agent_answering"
                ),
            ]
        ),
    )

    await handle_agent_setup_command(
        runtime,
        {
            "team_id": _TEAM_ID,
            "user_id": _USER_ID,
            "channel_id": _CHANNEL_ID,
            "trigger_id": "TRIG_CMD",
        },
    )

    mock = fake_slack_web_client.mock
    assert _sent(mock, _VIEWS_OPEN_KEY), "the loading modal must open on the fresh trigger_id"
    updates = _sent(mock, _VIEWS_UPDATE_KEY)
    assert len(updates) == 1, "the loading modal is replaced exactly once"
    rendered = json.dumps(updates[0]["view"])
    assert rendered.index(_ANSWERING_AGENT) < rendered.index(_OTHER_AGENT), (
        "the responder for this channel is rendered before the others"
    )


async def test_command_marks_only_the_agent_no_tier_routes_to_as_unrouted(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
) -> None:
    """ "Not answering in any channel yet" is about the install, not about here.

    An agent that answers in another channel is reachable by the people in it;
    telling the reader it answers nowhere would send them to an admin for
    routing it already has.
    """
    tenant_id, fernet_key = await _seed_team(db_session)
    await db_session.commit()
    await _mark_tenant_default(db_session_factory, tenant_id=tenant_id, agent_name=_ANSWERING_AGENT)
    async with db_session_factory() as session, session.begin():
        await set_fields(
            session,
            scope=ChannelScopeRef(tenant_id=tenant_id, channel_id="C_ELSEWHERE"),
            tenant_id=tenant_id,
            agent_name=_ROUTED_ELSEWHERE_AGENT,
            mode="agent",
        )

    runtime = _build_runtime(
        fernet_key,
        db_session_factory,
        handler=_ma_handler(
            [
                _agent_payload(
                    tenant_id=tenant_id, name=_ANSWERING_AGENT, agent_id="agent_answering"
                ),
                _agent_payload(
                    tenant_id=tenant_id,
                    name=_ROUTED_ELSEWHERE_AGENT,
                    agent_id="agent_elsewhere",
                ),
                _agent_payload(tenant_id=tenant_id, name=_OTHER_AGENT, agent_id="agent_other"),
            ]
        ),
    )

    await handle_agent_setup_command(
        runtime,
        {
            "team_id": _TEAM_ID,
            "user_id": _USER_ID,
            "channel_id": _CHANNEL_ID,
            "trigger_id": "TRIG_CMD",
        },
    )

    blocks = _sent(fake_slack_web_client.mock, _VIEWS_UPDATE_KEY)[0]["view"]["blocks"]
    assert "Not assigned" in _roster_row(blocks, _OTHER_AGENT), (
        "the agent no tier routes to carries one clear routing status"
    )
    assert "Answers in another channel" in _roster_row(blocks, _ROUTED_ELSEWHERE_AGENT), (
        "an agent routed in another channel is reachable, and says so instead"
    )
    assert "Not assigned" not in _roster_row(blocks, _ROUTED_ELSEWHERE_AGENT), (
        "a routed agent must never be described as answering nowhere"
    )


# ---------------------------------------------------------------------------
# Paging
# ---------------------------------------------------------------------------


async def test_page_action_calls_views_update_with_view_id_and_hash_never_push(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
) -> None:
    """Paging re-renders the view it was clicked on; it never grows the stack.

    Slack allows three simultaneous views, so a pager that pushed would spend
    the budget after two clicks.
    """
    tenant_id, fernet_key = await _seed_team(db_session)
    await db_session.commit()

    agents = [
        _agent_payload(tenant_id=tenant_id, name=f"agent-{index:03d}", agent_id=f"agent_{index}")
        for index in range(45)
    ]
    runtime = _build_runtime(fernet_key, db_session_factory, handler=_ma_handler(agents))

    await handle_agent_setup_action(
        runtime, _action_payload(ACTION_PAGE_NEXT, meta=_meta(view="agents", page=0))
    )

    mock = fake_slack_web_client.mock
    assert _VIEWS_PUSH_KEY not in mock.requests, "a pager must never push a view"
    updates = _sent(mock, _VIEWS_UPDATE_KEY)
    assert len(updates) == 1, "exactly one in-place update per page click"
    assert updates[0]["view_id"] == _ROOT_VIEW_ID, "the page lands on the clicked view"
    assert updates[0]["hash"] == _VIEW_HASH, (
        "the view hash is sent so a racing second click loses instead of overwriting"
    )
    updated_meta = decode_panel_metadata(updates[0]["view"]["private_metadata"])
    assert updated_meta is not None and updated_meta.page == 1, (
        "the rendered view carries the page it is actually showing"
    )


async def test_page_action_when_hash_conflict_logs_and_does_not_raise(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
) -> None:
    """A hash conflict means the view already moved on; the click is dropped.

    Anything louder would report a failure for a render that is already
    superseded on screen.
    """
    tenant_id, fernet_key = await _seed_team(db_session)
    await db_session.commit()

    agents = [
        _agent_payload(tenant_id=tenant_id, name=f"agent-{index:03d}", agent_id=f"agent_{index}")
        for index in range(45)
    ]
    runtime = _build_runtime(fernet_key, db_session_factory, handler=_ma_handler(agents))
    _override(fake_slack_web_client.mock, "views.update", {"ok": False, "error": "hash_conflict"})

    await handle_agent_setup_action(
        runtime, _action_payload(ACTION_PAGE_NEXT, meta=_meta(view="agents", page=0))
    )

    updates = _sent(fake_slack_web_client.mock, _VIEWS_UPDATE_KEY)
    assert len(updates) == 1, "the conflicting update is attempted once and then dropped"


async def test_page_action_when_roster_read_fails_renders_error_view_not_silence(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
) -> None:
    """A failed read replaces the view's content instead of doing nothing.

    A click that appears to do nothing invites a second click; the modal says
    what happened and carries a reference.
    """
    _tenant_id, fernet_key = await _seed_team(db_session)
    await db_session.commit()

    runtime = _build_runtime(
        fernet_key, db_session_factory, handler=_ma_handler([], fail_list=True)
    )

    await handle_agent_setup_action(
        runtime, _action_payload(ACTION_PAGE_NEXT, meta=_meta(view="agents", page=0))
    )

    updates = _sent(fake_slack_web_client.mock, _VIEWS_UPDATE_KEY)
    assert updates, "a failed read must still render something into the open view"
    assert "load agent setup" in json.dumps(updates[-1]["view"]), (
        "the last thing rendered is the error view, not a stale roster"
    )


# ---------------------------------------------------------------------------
# Navigation
# ---------------------------------------------------------------------------


async def test_details_action_pushes_once_and_carries_agent_in_metadata(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
) -> None:
    """Details is a push, and the pushed view names its own target."""
    tenant_id, fernet_key = await _seed_team(db_session)
    await db_session.commit()

    runtime = _build_runtime(
        fernet_key,
        db_session_factory,
        handler=_ma_handler(
            [_agent_payload(tenant_id=tenant_id, name=_OTHER_AGENT, agent_id="agent_other")]
        ),
    )

    await handle_agent_setup_action(
        runtime, _action_payload(ACTION_DETAILS, meta=_meta(), value=_OTHER_AGENT)
    )

    mock = fake_slack_web_client.mock
    pushes = _sent(mock, _VIEWS_PUSH_KEY)
    assert len(pushes) == 1, "opening Details pushes exactly one view"
    assert _VIEWS_UPDATE_KEY not in mock.requests, "the root view is left as it was"
    pushed_meta = decode_panel_metadata(pushes[0]["view"]["private_metadata"])
    assert pushed_meta is not None, "the pushed view carries typed panel metadata"
    assert (pushed_meta.view, pushed_meta.agent_name) == ("details", _OTHER_AGENT), (
        "the pushed view knows which agent it is showing"
    )


async def test_routing_action_pushes_from_root_and_details_view_has_no_routing_action(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
) -> None:
    """Who-answers-where is reachable from the root only.

    Depth two is the last view Slack will show, so Details offers no further
    push — otherwise Back would be the only way out of a dead end.
    """
    tenant_id, fernet_key = await _seed_team(db_session)
    await db_session.commit()

    runtime = _build_runtime(
        fernet_key,
        db_session_factory,
        handler=_ma_handler(
            [_agent_payload(tenant_id=tenant_id, name=_OTHER_AGENT, agent_id="agent_other")]
        ),
    )

    await handle_agent_setup_action(runtime, _action_payload(ACTION_ROUTING, meta=_meta()))
    await handle_agent_setup_action(
        runtime, _action_payload(ACTION_DETAILS, meta=_meta(), value=_OTHER_AGENT)
    )

    pushes = _sent(fake_slack_web_client.mock, _VIEWS_PUSH_KEY)
    assert len(pushes) == 2, "each navigation from the root pushes one view"
    routing_meta = decode_panel_metadata(pushes[0]["view"]["private_metadata"])
    assert routing_meta is not None and routing_meta.view == "routing", (
        "the first push is the routing view"
    )
    assert ACTION_ROUTING not in json.dumps(pushes[1]["view"]), "Details offers no third level"


async def test_new_action_pushes_form_with_root_view_id(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
) -> None:
    """The New agent form remembers the root so creation can refresh it."""
    _tenant_id, fernet_key = await _seed_team(db_session)
    await db_session.commit()

    runtime = _build_runtime(fernet_key, db_session_factory, handler=_ma_handler([]))

    await handle_agent_setup_action(runtime, _action_payload(ACTION_NEW, meta=_meta(page=1)))

    pushes = _sent(fake_slack_web_client.mock, _VIEWS_PUSH_KEY)
    assert len(pushes) == 1, "New agent pushes the form"
    form_meta = decode_panel_metadata(pushes[0]["view"]["private_metadata"])
    assert form_meta is not None, "the form carries typed panel metadata"
    assert form_meta.root_view_id == _ROOT_VIEW_ID, (
        "the form records the root view so the created agent shows up behind it"
    )
    assert form_meta.view == "new_agent", "the pushed view is the creation form"


async def test_expand_keys_updates_details_in_place(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
) -> None:
    """Show all keys re-renders Details where it stands, expansion recorded."""
    tenant_id, fernet_key = await _seed_team(db_session)
    await db_session.commit()

    runtime = _build_runtime(
        fernet_key,
        db_session_factory,
        handler=_ma_handler(
            [_agent_payload(tenant_id=tenant_id, name=_OTHER_AGENT, agent_id="agent_other")]
        ),
    )

    await handle_agent_setup_action(
        runtime,
        _action_payload(
            ACTION_EXPAND_KEYS,
            meta=_meta(view="details", agent_name=_OTHER_AGENT),
            view_id="V_DETAILS",
        ),
    )

    mock = fake_slack_web_client.mock
    assert _VIEWS_PUSH_KEY not in mock.requests, "an expansion never pushes a view"
    updates = _sent(mock, _VIEWS_UPDATE_KEY)
    assert len(updates) == 1, "the Details view is re-rendered once"
    assert updates[0]["view_id"] == "V_DETAILS", "the expansion lands on the Details view"
    assert updates[0]["hash"] == _VIEW_HASH, "the expansion protects its in-place update"
    updated_meta = decode_panel_metadata(updates[0]["view"]["private_metadata"])
    assert updated_meta is not None and updated_meta.expanded == "keys", (
        "the re-render records that keys are expanded, so the toggle can close again"
    )


async def test_expanding_connections_updates_details_without_growing_the_stack(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
) -> None:
    tenant_id, fernet_key = await _seed_team(db_session)
    await db_session.commit()
    runtime = _build_runtime(
        fernet_key,
        db_session_factory,
        handler=_ma_handler(
            [_agent_payload(tenant_id=tenant_id, name=_OTHER_AGENT, agent_id="agent_other")]
        ),
    )

    await handle_agent_setup_action(
        runtime,
        _action_payload(
            ACTION_EXPAND_CONNECTIONS,
            meta=_meta(view="details", agent_name=_OTHER_AGENT, expanded="keys"),
            view_id="V_DETAILS",
        ),
    )

    mock = fake_slack_web_client.mock
    assert _VIEWS_PUSH_KEY not in mock.requests, "list expansion never adds modal depth"
    updates = _sent(mock, _VIEWS_UPDATE_KEY)
    assert len(updates) == 1, "the current Details view updates once"
    assert updates[0]["hash"] == _VIEW_HASH, "every expansion sends the current view hash"
    updated_meta = decode_panel_metadata(updates[0]["view"]["private_metadata"])
    assert updated_meta is not None and updated_meta.expanded == "connections", (
        "opening Connections closes the previously expanded Keys list"
    )


@pytest.mark.parametrize("action_id", [ACTION_EXPAND_KEYS, ACTION_EXPAND_CONNECTIONS])
async def test_expansion_hash_conflict_is_dropped(
    action_id: str,
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
) -> None:
    tenant_id, fernet_key = await _seed_team(db_session)
    await db_session.commit()
    runtime = _build_runtime(
        fernet_key,
        db_session_factory,
        handler=_ma_handler(
            [_agent_payload(tenant_id=tenant_id, name=_OTHER_AGENT, agent_id="agent_other")]
        ),
    )
    _override(fake_slack_web_client.mock, "views.update", {"ok": False, "error": "hash_conflict"})

    await handle_agent_setup_action(
        runtime,
        _action_payload(
            action_id,
            meta=_meta(view="details", agent_name=_OTHER_AGENT),
            view_id="V_DETAILS",
            view_hash="H_DETAILS_STALE",
        ),
    )

    updates = _sent(fake_slack_web_client.mock, _VIEWS_UPDATE_KEY)
    assert len(updates) == 1, "the conflicting expansion is attempted once and then dropped"
    assert updates[0]["hash"] == "H_DETAILS_STALE"


async def test_stale_expansion_fallback_uses_current_view_hash(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
) -> None:
    _tenant_id, fernet_key = await _seed_team(db_session)
    await db_session.commit()
    runtime = _build_runtime(fernet_key, db_session_factory, handler=_ma_handler([]))

    await handle_agent_setup_action(
        runtime,
        _action_payload(
            ACTION_EXPAND_CONNECTIONS,
            meta=_meta(view="details", agent_name=_OTHER_AGENT),
            view_id="V_DETAILS",
            view_hash="H_DETAILS_CURRENT",
        ),
    )

    updates = _sent(fake_slack_web_client.mock, _VIEWS_UPDATE_KEY)
    assert len(updates) == 1, "the missing target falls back to one in-place roster update"
    assert updates[0]["hash"] == "H_DETAILS_CURRENT"
    assert "no longer available" in json.dumps(updates[0]["view"])


async def test_coding_tools_action_when_unconfigured_posts_a_note_and_mints_nothing(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
) -> None:
    """A deployment without an MCP URL says so rather than failing silently."""
    tenant_id, fernet_key = await _seed_team(db_session)
    await db_session.commit()

    runtime = _build_runtime(
        fernet_key,
        db_session_factory,
        handler=_ma_handler(
            [_agent_payload(tenant_id=tenant_id, name=_OTHER_AGENT, agent_id="agent_other")]
        ),
    )

    await handle_agent_setup_action(
        runtime,
        _action_payload(
            ACTION_CODING_TOOLS,
            meta=_meta(view="details", agent_name=_OTHER_AGENT),
            value=_OTHER_AGENT,
            view_id="V_DETAILS",
        ),
    )

    ephemerals = _sent(
        fake_slack_web_client.mock,
        ("POST", yarl.URL(f"{_SLACK_API_BASE}/chat.postEphemeral")),
    )
    assert len(ephemerals) == 1, "the click is answered even when nothing can be minted"
    assert "not set up" in ephemerals[0]["text"], "the note names the missing deployment setup"


async def test_revoke_click_routes_without_a_view_behind_it(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
) -> None:
    """The revoke button lives on an ephemeral, so its payload carries no view.

    Routing it like the in-view panel actions would drop the click silently and
    leave a live token with a dead button beside it.
    """
    tenant_id, fernet_key = await _seed_team(db_session)
    await db_session.commit()
    posts: list[tuple[str, dict[str, Any]]] = []

    class _RecordingHttpClient:
        async def post(self, url: str, *, json: dict[str, Any]) -> httpx.Response:
            posts.append((url, json))
            return httpx.Response(200, text="ok")

    runtime = _build_runtime(fernet_key, db_session_factory, handler=_ma_handler([]))
    object.__setattr__(runtime, "http_client", _RecordingHttpClient())

    await handle_agent_setup_action(
        runtime,
        {
            "team": {"id": _TEAM_ID},
            "user": {"id": _USER_ID},
            "channel": {"id": _CHANNEL_ID},
            "response_url": "https://hooks.slack.test/actions/response",
            "actions": [
                {"action_id": ACTION_REVOKE_TOKEN, "value": str(uuid.uuid4())},
            ],
        },
    )

    assert tenant_id is not None, "the tenant is seeded so the handler can resolve a token"
    assert len(posts) == 1, "the click is answered on the message it came from"
    assert posts[0][1]["replace_original"] is False, (
        "an unknown token refuses rather than replacing the message"
    )


async def test_conversation_action_from_details_targets_that_agent(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
) -> None:
    """Manage, clicked in Details, configures the agent shown there.

    The button carries the MA id of the agent beside it, so the setup thread
    is created against that agent rather than the channel's responder.
    """
    tenant_id, fernet_key = await _seed_team(db_session)
    await db_session.commit()

    responder = _agent_payload(tenant_id=tenant_id, name="daimon", agent_id="agent_daimon")
    responder["metadata"]["daimon_managed"] = "true"
    target = _agent_payload(tenant_id=tenant_id, name=_OTHER_AGENT, agent_id="agent_other")
    runtime = _build_runtime(
        fernet_key,
        db_session_factory,
        handler=_ma_handler([responder, target]),
        deployment_default=DeploymentDefault(agent_name="daimon"),
    )

    mock = fake_slack_web_client.mock
    _override(mock, "auth.test", {"ok": True, "user_id": "U_BOT"})
    mock.get(  # pyright: ignore[reportUnknownMemberType]
        re.compile(r"https://slack\.com/api/chat\.getPermalink.*"),
        payload={
            "ok": True,
            "permalink": "https://slack.test/archives/C/p1?thread_ts=1000000000.000001",
        },
        repeat=True,
    )

    await handle_agent_setup_action(
        runtime,
        _action_payload(
            "agent_setup__conversation",
            meta=_meta(view="details", agent_name=_OTHER_AGENT, channel_id="C_PARENT"),
            value="agent_other",
            view_id="V_DETAILS",
        ),
    )

    async with db_session_factory() as session:
        binding = await get_binding(
            session,
            tenant_id=tenant_id,
            platform="slack",
            parent_channel_id="C_PARENT",
            thread_id="1000000000.000001",
        )
    assert binding is not None, "the click creates a setup conversation"
    assert binding.configuration_target_name == _OTHER_AGENT, (
        "the conversation configures the agent Details was showing"
    )


async def test_channel_admins_click_refuses_a_member(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
) -> None:
    """The rendered button is a hint: a member's click is refused and no form opens."""
    _, fernet_key = await _seed_team(db_session)
    await db_session.commit()
    runtime = _build_runtime(fernet_key, db_session_factory, handler=_ma_handler([]))

    await handle_agent_setup_action(
        runtime,
        _action_payload(ACTION_CHANNEL_ADMINS, meta=_meta(view="routing"), view_id="V_ROUTING"),
    )

    ephemerals = _sent(
        fake_slack_web_client.mock,
        ("POST", yarl.URL(f"{_SLACK_API_BASE}/chat.postEphemeral")),
    )
    assert [e["text"] for e in ephemerals] == [CHANNEL_ADMINS_NEED_ADMIN_MESSAGE], (
        "the member is told only a workspace admin may do this"
    )
    assert _sent(fake_slack_web_client.mock, _VIEWS_PUSH_KEY) == [], "no form is pushed"


async def test_isolation_click_refuses_a_member(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
) -> None:
    """A member's click on a rendered isolation button is refused and changes nothing."""
    tenant_id, fernet_key = await _seed_team(db_session)
    await db_session.commit()
    runtime = _build_runtime(fernet_key, db_session_factory, handler=_ma_handler([]))

    await handle_agent_setup_action(
        runtime,
        _action_payload(ACTION_ISOLATE, meta=_meta(view="routing"), view_id="V_ROUTING"),
    )

    ephemerals = _sent(
        fake_slack_web_client.mock,
        ("POST", yarl.URL(f"{_SLACK_API_BASE}/chat.postEphemeral")),
    )
    assert [e["text"] for e in ephemerals] == [ISOLATION_NEED_ADMIN_MESSAGE]
    async with db_session_factory() as session:
        policy = await load_access_policy(session, tenant_id=tenant_id)
    assert policy.isolated_channel_ids == (), "nothing was isolated"


async def test_isolation_click_refuses_a_shared_agent_then_isolates_with_a_copy(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from daimon.adapters.slack.agent_setup import actions

    tenant_id, fernet_key = await _seed_team(db_session)
    room = "C0TEAMALPHA"
    for channel in (room, "C0ELSEWHERE"):
        await set_fields(
            db_session,
            scope=ChannelScopeRef(tenant_id=tenant_id, channel_id=channel),
            tenant_id=tenant_id,
            agent_name="shared",
            mode="agent",
        )
    await db_session.commit()
    state = FakeMAState()
    agent = ma_agent(id="agent_shared", name="shared", tenant_id=tenant_id)
    state.agents[agent.id] = agent.model_dump(mode="json")

    def environments(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/v1/environments":
            return httpx.Response(200, json={"data": [], "has_more": False})
        raise NotHandled

    runtime = _build_runtime(
        fernet_key,
        db_session_factory,
        handler=combine_handlers(environments, make_fake_ma_handler(state)),
    )
    monkeypatch.setattr(actions, "resolve_is_admin", AsyncMock(return_value=True))
    mock = fake_slack_web_client.mock
    mock.get(  # pyright: ignore[reportUnknownMemberType]
        re.compile(r"https://slack\.com/api/conversations\.info.*"),
        payload={"ok": True, "channel": {"id": room, "name": "Team Alpha"}},
        repeat=True,
    )

    for action_id in (ACTION_ISOLATE, ACTION_ISOLATE_COPY):
        await handle_agent_setup_action(
            runtime,
            _action_payload(
                action_id, meta=_meta(view="routing", channel_id=room), view_id="V_ROUTING"
            ),
        )

    texts = [
        e["text"] for e in _sent(mock, ("POST", yarl.URL(f"{_SLACK_API_BASE}/chat.postEphemeral")))
    ]
    assert "also answers outside this channel" in texts[0], "the plain click says why not"
    assert "*team-alpha*, a copy of *shared*" in texts[1], "the copy click makes one"
    async with db_session_factory() as session:
        policy = await load_access_policy(session, tenant_id=tenant_id)
    assert policy.isolated_channel_ids == (room,)
    assert len(_sent(mock, _VIEWS_UPDATE_KEY)) == 2, "each click refreshes Who answers where"

    await handle_agent_setup_action(
        runtime,
        _action_payload(
            ACTION_LIFT_ISOLATION, meta=_meta(view="routing", channel_id=room), view_id="V_ROUTING"
        ),
    )
    texts = [
        e["text"] for e in _sent(mock, ("POST", yarl.URL(f"{_SLACK_API_BASE}/chat.postEphemeral")))
    ]
    assert "no longer private" in texts[2], "lifting says the agent may answer elsewhere"
    async with db_session_factory() as session:
        policy = await load_access_policy(session, tenant_id=tenant_id)
    assert (policy.isolated_channel_ids, policy.sealed_channel_ids) == ((), ()), "all lifted"
    assert "team-alpha" not in policy.agent_channel_pins, "the copy's pin is lifted"


async def test_add_skill_opens_the_form_or_refuses_by_where_the_agent_answers(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
) -> None:
    """A member may add to an agent that answers nowhere, not to the workspace default."""
    tenant_id, fernet_key = await _seed_team(db_session)
    await db_session.commit()
    agent = _agent_payload(tenant_id=tenant_id, name=_OTHER_AGENT, agent_id="agent_other")
    agent["metadata"]["daimon_account"] = str(uuid.uuid4())
    runtime = _build_runtime(fernet_key, db_session_factory, handler=_ma_handler([agent]))
    click = _action_payload(
        ACTION_ADD_SKILL,
        meta=_meta(view="details", agent_name=_OTHER_AGENT),
        value=_OTHER_AGENT,
        view_id="V_DETAILS",
    )
    mock = fake_slack_web_client.mock

    await handle_agent_setup_action(runtime, click)
    (pushed,) = _sent(mock, _VIEWS_PUSH_KEY)
    assert pushed["view"]["callback_id"] == CALLBACK_ADD_SKILL
    meta = decode_panel_metadata(pushed["view"]["private_metadata"])
    assert meta is not None and (meta.agent_name, meta.root_view_id) == (_OTHER_AGENT, "V_DETAILS")

    await _mark_tenant_default(db_session_factory, tenant_id=tenant_id, agent_name=_OTHER_AGENT)
    await handle_agent_setup_action(runtime, click)
    assert len(_sent(mock, _VIEWS_PUSH_KEY)) == 1, "the refused click pushes nothing"
    (refusal,) = _sent(mock, ("POST", yarl.URL(f"{_SLACK_API_BASE}/chat.postEphemeral")))
    assert "changing its skills needs a workspace admin" in refusal["text"]


async def test_add_skill_on_a_pinned_agent_opens_only_inside_its_channels(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
) -> None:
    """The panel's channel is the place: outside the pin a member's click is refused."""
    tenant_id, fernet_key = await _seed_team(db_session)
    await set_access_policy(
        db_session,
        tenant_id=tenant_id,
        policy=TenantAccessPolicy(agent_channel_pins={_OTHER_AGENT: ("C_ELSEWHERE",)}),
    )
    await db_session.commit()
    agent = _agent_payload(tenant_id=tenant_id, name=_OTHER_AGENT, agent_id="agent_other")
    agent["metadata"]["daimon_account"] = str(uuid.uuid4())
    runtime = _build_runtime(fernet_key, db_session_factory, handler=_ma_handler([agent]))
    mock = fake_slack_web_client.mock

    def click(channel_id: str) -> dict[str, Any]:
        return _action_payload(
            ACTION_ADD_SKILL,
            meta=_meta(view="details", agent_name=_OTHER_AGENT, channel_id=channel_id),
            value=_OTHER_AGENT,
            view_id="V_DETAILS",
        )

    await handle_agent_setup_action(runtime, click(_CHANNEL_ID))
    assert _sent(mock, _VIEWS_PUSH_KEY) == [], "refused outside the pin"
    (refusal,) = _sent(mock, ("POST", yarl.URL(f"{_SLACK_API_BASE}/chat.postEphemeral")))
    assert refusal["text"] == PIN_WRITE_REFUSAL

    await handle_agent_setup_action(runtime, click("C_ELSEWHERE"))
    assert len(_sent(mock, _VIEWS_PUSH_KEY)) == 1, "opens inside the pin"


async def test_add_skill_ignores_the_members_own_conversations_with_the_agent(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
) -> None:
    """A member's own live session is not someone else's use, as on Discord and in chat."""
    tenant_id, fernet_key = await _seed_team(db_session)
    principal = await get_or_create_platform_principal(
        db_session, platform="slack", external_id=_USER_ID, tenant_id=tenant_id
    )
    await create_thread_session(
        db_session,
        tenant_id=tenant_id,
        platform="slack",
        thread_id="1700000000.000100",
        account_id=principal.account_id,
        ma_session_id="sesn_own",
        ma_agent_id="agent_other",
    )
    await db_session.commit()
    agent = _agent_payload(tenant_id=tenant_id, name=_OTHER_AGENT, agent_id="agent_other")
    agent["metadata"]["daimon_account"] = str(uuid.uuid4())
    runtime = _build_runtime(fernet_key, db_session_factory, handler=_ma_handler([agent]))
    click = _action_payload(
        ACTION_ADD_SKILL,
        meta=_meta(view="details", agent_name=_OTHER_AGENT),
        value=_OTHER_AGENT,
        view_id="V_DETAILS",
    )

    await handle_agent_setup_action(runtime, click)

    assert len(_sent(fake_slack_web_client.mock, _VIEWS_PUSH_KEY)) == 1, "the form opens"
