"""The Slack coding keys list: who sees which keys, and who can revoke them."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
import yarl
from aioresponses import aioresponses as AioResponsesMock
from daimon.adapters.slack.agent_setup import coding_keys
from daimon.adapters.slack.agent_setup.panel_views import (
    ACTION_CODING_KEYS,
    ACTION_REVOKE_CODING_KEY,
)
from daimon.adapters.slack.agent_setup.state import PanelMetadata
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.mcp_auth import mint_agent_mcp_token, token_jti
from daimon.core.stores.agent_owners import set_owner
from daimon.core.stores.identity import get_or_create_platform_principal
from daimon.core.stores.mcp_tokens import get_mcp_token
from daimon.testing.factories import make_tenant
from slack_sdk.web.async_client import AsyncWebClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .test_coding_tools import (
    _AGENT_NAME,  # pyright: ignore[reportPrivateUsage]
    _CHANNEL_ID,  # pyright: ignore[reportPrivateUsage]
    _MA_AGENT_ID,  # pyright: ignore[reportPrivateUsage]
    _SLACK_API_BASE,  # pyright: ignore[reportPrivateUsage]
    _TEAM_ID,  # pyright: ignore[reportPrivateUsage]
    _USERS_INFO_PATTERN,  # pyright: ignore[reportPrivateUsage]
    _build_runtime,  # pyright: ignore[reportPrivateUsage]
    _ephemerals,  # pyright: ignore[reportPrivateUsage]
    _users_info_payload,  # pyright: ignore[reportPrivateUsage]
)

_OWNER = "U_CODING_TOOLS"
_ADMIN = "U_ADMIN"
_MEMBER = "U_MEMBER"
_VIEWS_PUSH = ("POST", yarl.URL(f"{_SLACK_API_BASE}/views.push"))
_VIEWS_UPDATE = ("POST", yarl.URL(f"{_SLACK_API_BASE}/views.update"))


class _World:
    def __init__(self, db_factory: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID) -> None:
        self.factory = db_factory
        self.tenant_id = tenant_id
        self.runtime = _build_runtime(db_factory, tenant_id=tenant_id)
        self.meta = PanelMetadata(
            team_id=_TEAM_ID, channel_id=_CHANNEL_ID, view="details", agent_name=_AGENT_NAME
        )

    async def account(self, user_id: str) -> uuid.UUID:
        async with self.factory.begin() as session:
            principal = await get_or_create_platform_principal(
                session, platform="slack", external_id=user_id, tenant_id=self.tenant_id
            )
        return principal.account_id

    async def build(self) -> None:
        self.owner = await self.account(_OWNER)
        self.admin = await self.account(_ADMIN)
        async with self.factory.begin() as session:
            await set_owner(
                session,
                tenant_id=self.tenant_id,
                agent_id=derive_agent_uuid(tenant_id=self.tenant_id, ma_agent_id=_MA_AGENT_ID),
                owner_account_id=self.owner,
                source="admin_assigned",
            )
        self.owner_key = await self.key(self.owner)
        self.admin_key = await self.key(self.admin)
        self.elsewhere_key = await self.key(self.owner, ma_agent_id="agent_other")

    async def key(self, account_id: uuid.UUID, *, ma_agent_id: str = _MA_AGENT_ID) -> uuid.UUID:
        async with self.factory.begin() as session:
            return token_jti(
                await mint_agent_mcp_token(
                    session,
                    account_id=account_id,
                    tenant_id=self.tenant_id,
                    agent_id=derive_agent_uuid(tenant_id=self.tenant_id, ma_agent_id=ma_agent_id),
                    label=_AGENT_NAME,
                    secret=b"jwt-secret",
                    now=datetime.now(UTC),
                )
            )

    async def press(
        self, action_id: str, *, user_id: str, is_admin: bool, value: str = _AGENT_NAME
    ) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
        """Press a button; the view Slack was sent (pushed or updated), and any ephemerals."""
        with AioResponsesMock() as mock:
            mock.get(  # pyright: ignore[reportUnknownMemberType]
                _USERS_INFO_PATTERN, payload=_users_info_payload(is_admin=is_admin), repeat=True
            )
            for method in ("chat.postEphemeral", "views.push", "views.update"):
                mock.post(f"{_SLACK_API_BASE}/{method}", payload={"ok": True}, repeat=True)  # pyright: ignore[reportUnknownMemberType]
            handled = await coding_keys.handle(
                self.runtime,
                AsyncWebClient(token="xoxb-test"),
                {"trigger_id": "TRIG", "view": {"id": "V1"}},
                action={"action_id": action_id, "value": value},
                meta=self.meta,
                team_id=_TEAM_ID,
                user_id=user_id,
            )
            assert handled
            sent = [
                call for key in (_VIEWS_PUSH, _VIEWS_UPDATE) for call in mock.requests.get(key, [])
            ]
            view = None
            if sent:
                body = dict(sent[-1][1].get("json") or sent[-1][1].get("data") or {})
                raw = body["view"]
                view = json.loads(raw) if isinstance(raw, str) else raw
            return view, _ephemerals(mock)


@pytest.fixture
async def world(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> _World:
    tenant = await make_tenant(db_session, platform="slack", workspace_id=_TEAM_ID)
    await db_session.commit()
    built = _World(db_session_factory, tenant.id)
    await built.build()
    return built


def _revoke_values(view: dict[str, Any]) -> set[str]:
    return {
        block["accessory"]["value"]
        for block in view["blocks"]
        if block.get("accessory", {}).get("action_id") == ACTION_REVOKE_CODING_KEY
    }


async def test_an_owner_sees_only_their_own_keys(world: _World) -> None:
    view, _ = await world.press(ACTION_CODING_KEYS, user_id=_OWNER, is_admin=False)

    assert view is not None
    assert _revoke_values(view) == {str(world.owner_key)}
    assert "eyJ" not in json.dumps(view), "a listing never carries a token"


async def test_an_admin_sees_every_key_of_the_agent_and_none_of_another(world: _World) -> None:
    view, _ = await world.press(ACTION_CODING_KEYS, user_id=_ADMIN, is_admin=True)

    assert view is not None
    assert _revoke_values(view) == {str(world.owner_key), str(world.admin_key)}


async def test_another_member_cannot_open_the_list(world: _World) -> None:
    view, ephemerals = await world.press(ACTION_CODING_KEYS, user_id=_MEMBER, is_admin=False)

    assert view is None
    assert [e["text"] for e in ephemerals] == [coding_keys.CODING_KEYS_REFUSAL]


async def test_an_owner_revokes_their_own_key_and_the_list_refreshes(world: _World) -> None:
    view, _ = await world.press(
        ACTION_REVOKE_CODING_KEY, user_id=_OWNER, is_admin=False, value=str(world.owner_key)
    )

    async with world.factory() as session:
        row = await get_mcp_token(session, jti=world.owner_key)
    assert row is not None and row.revoked_at is not None
    assert view is not None and _revoke_values(view) == set(), "the revoked key leaves the list"


async def test_an_owner_cannot_revoke_an_admins_key_even_by_naming_it(world: _World) -> None:
    await world.press(
        ACTION_REVOKE_CODING_KEY, user_id=_OWNER, is_admin=False, value=str(world.admin_key)
    )

    async with world.factory() as session:
        row = await get_mcp_token(session, jti=world.admin_key)
    assert row is not None and row.revoked_at is None


async def test_an_admin_revokes_any_key_of_this_agent_but_not_another_agents(
    world: _World,
) -> None:
    for jti in (world.owner_key, world.elsewhere_key):
        await world.press(ACTION_REVOKE_CODING_KEY, user_id=_ADMIN, is_admin=True, value=str(jti))

    async with world.factory() as session:
        owner_row = await get_mcp_token(session, jti=world.owner_key)
        elsewhere = await get_mcp_token(session, jti=world.elsewhere_key)
    assert owner_row is not None and owner_row.revoked_at is not None
    assert elsewhere is not None and elsewhere.revoked_at is None


async def test_a_member_cannot_revoke_a_key(world: _World) -> None:
    await world.press(
        ACTION_REVOKE_CODING_KEY, user_id=_MEMBER, is_admin=False, value=str(world.owner_key)
    )

    async with world.factory() as session:
        row = await get_mcp_token(session, jti=world.owner_key)
    assert row is not None and row.revoked_at is None
