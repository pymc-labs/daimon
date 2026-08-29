"""Tests for daimon.adapters.slack.agent_setup.gate.

Covers refuse_if_reachable_and_not_admin's four behaviors:
  - admin caller -> proceed, no DB session opened
  - non-admin + unreachable agent -> proceed, no ephemeral posted
  - non-admin + reachable agent -> refused, exactly one ephemeral posted
  - any caller, including an admin, against a defaults-managed agent ->
    refused, pointing at forking as the editable path

and refuse_if_shared_and_not_admin's branch ordering, which differs in one
place: the admin short-circuit runs first, so an admin can still attach a repo
to the workspace's built-in agent.

All cases use a real Postgres schema (db_session_factory) + the
transport-level FakeSlackWebClient from conftest (no AsyncMock on
client.* methods) for the Slack side, and real scoped_config_write rows
for reachability (never patching the predicate).
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Callable
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest
import yarl
from aioresponses import aioresponses as AioResponsesMock
from anthropic.types.beta import BetaManagedAgentsAgent
from daimon.adapters.slack.agent_setup.gate import (
    refuse_if_reachable_and_not_admin,
    refuse_if_shared_and_not_admin,
)
from daimon.adapters.slack.runtime import SlackRuntime
from daimon.core.defaults.metadata import (
    MA_METADATA_KEY_MANAGED,
    MA_METADATA_KEY_NAME,
    MA_METADATA_KEY_TENANT,
)
from daimon.testing.factories import make_tenant, make_tenant_config
from daimon.testing.ma import build_fake_anthropic, make_fake_ma_handler
from slack_sdk.web.async_client import AsyncWebClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_TEAM_ID = "T_GATE_TESTS"
_USER_ID = "U_GATE_TEST"
_CHANNEL_ID = "C_GATE_TEST"
_AGENT_NAME = "test-gate-agent"

_USERS_INFO_PATTERN = re.compile(r"https://slack\.com/api/users\.info.*")
_SLACK_API_BASE = "https://slack.com/api"


def _admin_users_info_payload(user_id: str = _USER_ID) -> dict[str, Any]:
    return {
        "ok": True,
        "user": {
            "id": user_id,
            "name": "admin",
            "is_admin": True,
            "is_owner": False,
            "is_primary_owner": False,
        },
    }


def _non_admin_users_info_payload(user_id: str = _USER_ID) -> dict[str, Any]:
    return {
        "ok": True,
        "user": {
            "id": user_id,
            "name": "member",
            "is_admin": False,
            "is_owner": False,
            "is_primary_owner": False,
        },
    }


def _build_runtime(db_factory: async_sessionmaker[AsyncSession]) -> SlackRuntime:
    """Construct a SlackRuntime with a fake Anthropic transport and real DB factory."""
    settings = MagicMock()
    return SlackRuntime(
        settings=settings,
        anthropic=build_fake_anthropic(make_fake_ma_handler()),
        sessionmaker=db_factory,
        billing_config=None,
        http_client=MagicMock(spec=httpx.AsyncClient),
        resolver_cache=MagicMock(),  # pyright: ignore[reportArgumentType]  # stub, turn path not exercised
        turn_deps=MagicMock(),  # pyright: ignore[reportArgumentType]  # stub, turn path not exercised
    )


def _runtime_with_failing_sessionmaker() -> SlackRuntime:
    """SlackRuntime whose sessionmaker raises if called -- proves the admin
    short-circuit never opens a DB session."""

    def _fail(*args: object, **kwargs: object) -> AsyncSession:
        raise AssertionError("sessionmaker must not be called when the caller is already admin")

    settings = MagicMock()
    return SlackRuntime(
        settings=settings,
        anthropic=build_fake_anthropic(make_fake_ma_handler()),
        sessionmaker=MagicMock(side_effect=_fail),  # pyright: ignore[reportArgumentType]
        billing_config=None,
        http_client=MagicMock(spec=httpx.AsyncClient),
        resolver_cache=MagicMock(),  # pyright: ignore[reportArgumentType]  # stub, turn path not exercised
        turn_deps=MagicMock(),  # pyright: ignore[reportArgumentType]  # stub, turn path not exercised
    )


@pytest.mark.asyncio
async def test_refuse_if_reachable_and_not_admin_when_admin_returns_false_and_opens_no_session() -> (
    None
):
    """Admin caller -> proceed (False), and the DB session is never opened."""
    with AioResponsesMock() as mock:
        mock.get(  # pyright: ignore[reportUnknownMemberType]
            _USERS_INFO_PATTERN,
            payload=_admin_users_info_payload(),
            repeat=True,
        )
        mock.post(  # pyright: ignore[reportUnknownMemberType]
            f"{_SLACK_API_BASE}/chat.postEphemeral",
            payload={"ok": True},
            repeat=True,
        )
        client = AsyncWebClient(token="xoxb-test")
        runtime = _runtime_with_failing_sessionmaker()

        refused = await refuse_if_reachable_and_not_admin(
            runtime,
            client,
            tenant_id=uuid.uuid4(),
            agent_name=_AGENT_NAME,
            channel_id=_CHANNEL_ID,
            user_id=_USER_ID,
        )

        assert refused is False, "an admin caller must never be refused"

        post_key = ("POST", yarl.URL(f"{_SLACK_API_BASE}/chat.postEphemeral"))
        assert post_key not in mock.requests, "admin path must not post any ephemeral"


@pytest.mark.asyncio
async def test_refuse_if_reachable_and_not_admin_when_non_admin_and_unreachable_returns_false(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Non-admin + unreachable target agent -> proceed (False), no ephemeral."""
    async with db_session_factory() as session:
        tenant = await make_tenant(session, platform="slack", workspace_id=_TEAM_ID)
        await session.commit()

    with AioResponsesMock() as mock:
        mock.get(  # pyright: ignore[reportUnknownMemberType]
            _USERS_INFO_PATTERN,
            payload=_non_admin_users_info_payload(),
            repeat=True,
        )
        mock.post(  # pyright: ignore[reportUnknownMemberType]
            f"{_SLACK_API_BASE}/chat.postEphemeral",
            payload={"ok": True},
            repeat=True,
        )
        client = AsyncWebClient(token="xoxb-test")
        runtime = _build_runtime(db_session_factory)

        refused = await refuse_if_reachable_and_not_admin(
            runtime,
            client,
            tenant_id=tenant.id,
            agent_name=_AGENT_NAME,
            channel_id=_CHANNEL_ID,
            user_id=_USER_ID,
        )

        assert refused is False, "a non-admin acting on an unreachable agent must proceed"

        post_key = ("POST", yarl.URL(f"{_SLACK_API_BASE}/chat.postEphemeral"))
        assert post_key not in mock.requests, "pass-through case must not post any ephemeral"


@pytest.mark.asyncio
async def test_refuse_if_reachable_and_not_admin_when_non_admin_and_reachable_returns_true(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Non-admin + a reachable (workspace-default) target agent -> refused (True),
    exactly one ephemeral posted."""
    async with db_session_factory() as session:
        tenant = await make_tenant(session, platform="slack", workspace_id=_TEAM_ID)
        await make_tenant_config(session, tenant=tenant, agent_name=_AGENT_NAME, mode="agent")
        await session.commit()

    with AioResponsesMock() as mock:
        mock.get(  # pyright: ignore[reportUnknownMemberType]
            _USERS_INFO_PATTERN,
            payload=_non_admin_users_info_payload(),
            repeat=True,
        )
        mock.post(  # pyright: ignore[reportUnknownMemberType]
            f"{_SLACK_API_BASE}/chat.postEphemeral",
            payload={"ok": True},
            repeat=True,
        )
        client = AsyncWebClient(token="xoxb-test")
        runtime = _build_runtime(db_session_factory)

        refused = await refuse_if_reachable_and_not_admin(
            runtime,
            client,
            tenant_id=tenant.id,
            agent_name=_AGENT_NAME,
            channel_id=_CHANNEL_ID,
            user_id=_USER_ID,
        )

        assert refused is True, "a non-admin acting on a reachable agent must be refused"

        post_key = ("POST", yarl.URL(f"{_SLACK_API_BASE}/chat.postEphemeral"))
        matches = mock.requests.get(post_key, [])
        assert len(matches) == 1, f"expected exactly one ephemeral, got {len(matches)}"

        _, kwargs = matches[0]
        body = kwargs.get("json") or {}
        assert "workspace-admin" in body.get("text", ""), (
            "ephemeral should name why the action was refused"
        )


def _seeded_agent_handler(
    *, tenant_id: uuid.UUID, agent_name: str
) -> Callable[[httpx.Request], httpx.Response]:
    """Serve a single defaults-managed agent from the agent-list endpoint."""
    payload = BetaManagedAgentsAgent(
        id="agent_seeded_0000000000",
        type="agent",
        name=agent_name,
        model={"id": "claude-sonnet-5"},  # type: ignore[arg-type]
        metadata={
            MA_METADATA_KEY_TENANT: str(tenant_id),
            MA_METADATA_KEY_NAME: agent_name,
            MA_METADATA_KEY_MANAGED: "true",
        },
        description=None,
        archived_at=None,
        created_at="2026-05-01T00:00:00Z",  # type: ignore[arg-type]
        updated_at="2026-05-01T00:00:00Z",  # type: ignore[arg-type]
        version=1,
        mcp_servers=[],
        skills=[],
        tools=[],
        system=None,
    ).model_dump(mode="json")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/v1/agents":
            return httpx.Response(200, json={"data": [payload], "has_more": False})
        return httpx.Response(404, json={"error": {"message": "unexpected route"}})

    return handler


async def test_gate_refuses_a_workspace_admin_editing_the_defaults_managed_agent(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A defaults-managed agent is off-limits to everyone, admins included.

    Editing one from the panel never stamps the reconciler's spec hash, so the
    edit would survive every later reconcile and drift the agent from the
    shipped defaults permanently. The admin short-circuit must not reach it.
    """
    async with db_session_factory() as session:
        tenant = await make_tenant(session, platform="slack", workspace_id=_TEAM_ID)
        await session.commit()

    with AioResponsesMock() as mock:
        mock.get(  # pyright: ignore[reportUnknownMemberType]
            _USERS_INFO_PATTERN,
            payload=_admin_users_info_payload(),
            repeat=True,
        )
        mock.post(  # pyright: ignore[reportUnknownMemberType]
            f"{_SLACK_API_BASE}/chat.postEphemeral",
            payload={"ok": True},
            repeat=True,
        )
        client = AsyncWebClient(token="xoxb-test")
        runtime = _build_runtime(db_session_factory)
        object.__setattr__(
            runtime,
            "anthropic",
            build_fake_anthropic(
                _seeded_agent_handler(tenant_id=tenant.id, agent_name=_AGENT_NAME)
            ),
        )

        refused = await refuse_if_reachable_and_not_admin(
            runtime,
            client,
            tenant_id=tenant.id,
            agent_name=_AGENT_NAME,
            channel_id=_CHANNEL_ID,
            user_id=_USER_ID,
        )

        assert refused is True, (
            "an admin must still be refused against the workspace's built-in agent"
        )

        post_key = ("POST", yarl.URL(f"{_SLACK_API_BASE}/chat.postEphemeral"))
        matches = mock.requests.get(post_key, [])
        assert len(matches) == 1, f"expected exactly one ephemeral, got {len(matches)}"

        _, kwargs = matches[0]
        text = (kwargs.get("json") or {}).get("text", "")
        assert "built-in agent" in text, "ephemeral should say the agent is built in"
        assert "Fork" in text, "ephemeral should point at forking as the editable path"


async def test_shared_gate_admin_passes_on_a_defaults_managed_agent(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """An admin may write attachments on the workspace's built-in agent.

    Binding a repo to the built-in agent is the first-run onboarding step, so
    the admin short-circuit has to run before the defaults-managed lookup. If
    the two are ever reordered the lookup fires, the agent reads as managed,
    and onboarding breaks -- which is what the MA call counter pins here.
    """
    async with db_session_factory() as session:
        tenant = await make_tenant(session, platform="slack", workspace_id=_TEAM_ID)
        await session.commit()

    ma_calls: list[str] = []
    serve_seeded_agent = _seeded_agent_handler(tenant_id=tenant.id, agent_name=_AGENT_NAME)

    def counting_handler(request: httpx.Request) -> httpx.Response:
        ma_calls.append(request.url.path)
        return serve_seeded_agent(request)

    with AioResponsesMock() as mock:
        mock.get(  # pyright: ignore[reportUnknownMemberType]
            _USERS_INFO_PATTERN,
            payload=_admin_users_info_payload(),
            repeat=True,
        )
        mock.post(  # pyright: ignore[reportUnknownMemberType]
            f"{_SLACK_API_BASE}/chat.postEphemeral",
            payload={"ok": True},
            repeat=True,
        )
        client = AsyncWebClient(token="xoxb-test")
        runtime = _runtime_with_failing_sessionmaker()
        object.__setattr__(runtime, "anthropic", build_fake_anthropic(counting_handler))

        refused = await refuse_if_shared_and_not_admin(
            runtime,
            client,
            tenant_id=tenant.id,
            agent_name=_AGENT_NAME,
            channel_id=_CHANNEL_ID,
            user_id=_USER_ID,
        )

        assert refused is False, (
            "an admin must still be able to attach a repo to the built-in agent"
        )
        assert ma_calls == [], "the admin short-circuit must precede the defaults-managed lookup"

        post_key = ("POST", yarl.URL(f"{_SLACK_API_BASE}/chat.postEphemeral"))
        assert post_key not in mock.requests, "admin path must not post any ephemeral"


async def test_shared_gate_admin_passes_on_a_reachable_agent_without_reading_the_database(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Admin caller on a workspace-default agent -> proceed, no DB read.

    The agent is genuinely reachable in the database, but the runtime handed
    to the gate raises if its sessionmaker is called, so a pass here proves the
    admin short-circuit returns before the reachability read.
    """
    async with db_session_factory() as session:
        tenant = await make_tenant(session, platform="slack", workspace_id=_TEAM_ID)
        await make_tenant_config(session, tenant=tenant, agent_name=_AGENT_NAME, mode="agent")
        await session.commit()

    with AioResponsesMock() as mock:
        mock.get(  # pyright: ignore[reportUnknownMemberType]
            _USERS_INFO_PATTERN,
            payload=_admin_users_info_payload(),
            repeat=True,
        )
        mock.post(  # pyright: ignore[reportUnknownMemberType]
            f"{_SLACK_API_BASE}/chat.postEphemeral",
            payload={"ok": True},
            repeat=True,
        )
        client = AsyncWebClient(token="xoxb-test")
        runtime = _runtime_with_failing_sessionmaker()

        refused = await refuse_if_shared_and_not_admin(
            runtime,
            client,
            tenant_id=tenant.id,
            agent_name=_AGENT_NAME,
            channel_id=_CHANNEL_ID,
            user_id=_USER_ID,
        )

        assert refused is False, "an admin caller must never be refused an attachment write"

        post_key = ("POST", yarl.URL(f"{_SLACK_API_BASE}/chat.postEphemeral"))
        assert post_key not in mock.requests, "admin path must not post any ephemeral"


async def test_shared_gate_non_admin_refuses_on_a_defaults_managed_agent(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Non-admin + the workspace's built-in agent -> refused, one ephemeral."""
    async with db_session_factory() as session:
        tenant = await make_tenant(session, platform="slack", workspace_id=_TEAM_ID)
        await session.commit()

    with AioResponsesMock() as mock:
        mock.get(  # pyright: ignore[reportUnknownMemberType]
            _USERS_INFO_PATTERN,
            payload=_non_admin_users_info_payload(),
            repeat=True,
        )
        mock.post(  # pyright: ignore[reportUnknownMemberType]
            f"{_SLACK_API_BASE}/chat.postEphemeral",
            payload={"ok": True},
            repeat=True,
        )
        client = AsyncWebClient(token="xoxb-test")
        runtime = _build_runtime(db_session_factory)
        object.__setattr__(
            runtime,
            "anthropic",
            build_fake_anthropic(
                _seeded_agent_handler(tenant_id=tenant.id, agent_name=_AGENT_NAME)
            ),
        )

        refused = await refuse_if_shared_and_not_admin(
            runtime,
            client,
            tenant_id=tenant.id,
            agent_name=_AGENT_NAME,
            channel_id=_CHANNEL_ID,
            user_id=_USER_ID,
        )

        assert refused is True, "a member must not rewrite the built-in agent's attachments"

        post_key = ("POST", yarl.URL(f"{_SLACK_API_BASE}/chat.postEphemeral"))
        matches = mock.requests.get(post_key, [])
        assert len(matches) == 1, f"expected exactly one ephemeral, got {len(matches)}"

        text = (matches[0][1].get("json") or {}).get("text", "")
        assert "workspace-admin permission" in text, (
            "ephemeral should name the permission the caller lacks"
        )


async def test_shared_gate_non_admin_refuses_on_a_reachable_agent(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Non-admin + a workspace-default agent -> refused, one ephemeral."""
    async with db_session_factory() as session:
        tenant = await make_tenant(session, platform="slack", workspace_id=_TEAM_ID)
        await make_tenant_config(session, tenant=tenant, agent_name=_AGENT_NAME, mode="agent")
        await session.commit()

    with AioResponsesMock() as mock:
        mock.get(  # pyright: ignore[reportUnknownMemberType]
            _USERS_INFO_PATTERN,
            payload=_non_admin_users_info_payload(),
            repeat=True,
        )
        mock.post(  # pyright: ignore[reportUnknownMemberType]
            f"{_SLACK_API_BASE}/chat.postEphemeral",
            payload={"ok": True},
            repeat=True,
        )
        client = AsyncWebClient(token="xoxb-test")
        runtime = _build_runtime(db_session_factory)

        refused = await refuse_if_shared_and_not_admin(
            runtime,
            client,
            tenant_id=tenant.id,
            agent_name=_AGENT_NAME,
            channel_id=_CHANNEL_ID,
            user_id=_USER_ID,
        )

        assert refused is True, (
            "a member must not repoint or overwrite a shared agent's attachments"
        )

        post_key = ("POST", yarl.URL(f"{_SLACK_API_BASE}/chat.postEphemeral"))
        matches = mock.requests.get(post_key, [])
        assert len(matches) == 1, f"expected exactly one ephemeral, got {len(matches)}"

        text = (matches[0][1].get("json") or {}).get("text", "")
        assert "workspace-admin permission" in text, (
            "ephemeral should name the permission the caller lacks"
        )


async def test_shared_gate_non_admin_passes_on_an_unreachable_unmanaged_agent(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Non-admin + an agent nothing resolves to -> proceed, no ephemeral.

    An unreachable, unmanaged agent is not shared state, so its owner may
    bind a repo or paste secrets without an admin.
    """
    async with db_session_factory() as session:
        tenant = await make_tenant(session, platform="slack", workspace_id=_TEAM_ID)
        await session.commit()

    with AioResponsesMock() as mock:
        mock.get(  # pyright: ignore[reportUnknownMemberType]
            _USERS_INFO_PATTERN,
            payload=_non_admin_users_info_payload(),
            repeat=True,
        )
        mock.post(  # pyright: ignore[reportUnknownMemberType]
            f"{_SLACK_API_BASE}/chat.postEphemeral",
            payload={"ok": True},
            repeat=True,
        )
        client = AsyncWebClient(token="xoxb-test")
        runtime = _build_runtime(db_session_factory)

        refused = await refuse_if_shared_and_not_admin(
            runtime,
            client,
            tenant_id=tenant.id,
            agent_name=_AGENT_NAME,
            channel_id=_CHANNEL_ID,
            user_id=_USER_ID,
        )

        assert refused is False, "an unshared agent's attachments stay member-writable"

        post_key = ("POST", yarl.URL(f"{_SLACK_API_BASE}/chat.postEphemeral"))
        assert post_key not in mock.requests, "pass-through case must not post any ephemeral"
