"""The Details panel's Coding keys screen: who sees which keys, and who can revoke them."""

from __future__ import annotations

import datetime as dt
import uuid
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from daimon.adapters.discord.agent_setup.coding_keys_view import (
    CODING_KEYS_REFUSAL,
    CodingKeysView,
    load_coding_keys_view,
)
from daimon.adapters.discord.agent_setup.details_view import DetailsView
from daimon.core.ma_identity import derive_agent_uuid, derive_tenant_uuid
from daimon.core.mcp_auth import mint_agent_mcp_token, token_jti
from daimon.core.stores.agent_owners import set_owner
from daimon.core.stores.mcp_tokens import get_mcp_token
from daimon.testing.factories import make_account, make_platform_principal, make_tenant
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .test_details_view import (
    _admin_interaction,  # pyright: ignore[reportPrivateUsage]
    _details,  # pyright: ignore[reportPrivateUsage]
    _find_button,  # pyright: ignore[reportPrivateUsage]
    _make_runtime,  # pyright: ignore[reportPrivateUsage]
    _member_interaction,  # pyright: ignore[reportPrivateUsage]
    _roster_agent,  # pyright: ignore[reportPrivateUsage]
    _state,  # pyright: ignore[reportPrivateUsage]
    _walk,  # pyright: ignore[reportPrivateUsage]
)

_MA_ID = "ag_research"
_SECRET = b"s" * 32


class _World:
    def __init__(self, db: AsyncSession, factory: async_sessionmaker[AsyncSession]) -> None:
        self.db = db
        self.runtime = _make_runtime(factory)

    async def build(self) -> None:
        db = self.db
        self.tenant = await make_tenant(
            db,
            platform="discord",
            workspace_id="2001",
            id=derive_tenant_uuid(platform="discord", workspace_id="2001"),
        )
        self.agent_id = derive_agent_uuid(tenant_id=self.tenant.id, ma_agent_id=_MA_ID)
        self.other_agent_id = derive_agent_uuid(tenant_id=self.tenant.id, ma_agent_id="ag_other")
        self.owner = await make_account(db, tenant=self.tenant)
        self.admin = await make_account(db, tenant=self.tenant)
        self.member = await make_account(db, tenant=self.tenant)
        await make_platform_principal(
            db, platform="discord", external_id="42", tenant=self.tenant, account=self.owner
        )
        await set_owner(
            db,
            tenant_id=self.tenant.id,
            agent_id=self.agent_id,
            owner_account_id=self.owner.id,
            source="admin_assigned",
        )
        self.owner_key = await self.key(self.owner.id)
        self.owner_key_2 = await self.key(self.owner.id)
        self.admin_key = await self.key(self.admin.id)
        self.elsewhere_key = await self.key(self.owner.id, agent=self.other_agent_id)

    async def key(self, account_id: uuid.UUID, *, agent: uuid.UUID | None = None) -> uuid.UUID:
        return token_jti(
            await mint_agent_mcp_token(
                self.db,
                account_id=account_id,
                tenant_id=self.tenant.id,
                agent_id=agent or self.agent_id,
                label="research-bot",
                secret=_SECRET,
                now=dt.datetime.now(dt.UTC),
            )
        )

    async def view(self, *, as_account: uuid.UUID, interaction: MagicMock) -> CodingKeysView | None:
        await self.db.commit()
        return await load_coding_keys_view(
            interaction,
            runtime=self.runtime,
            state=_state(_details(), account_id=as_account, is_admin=False),
            allowed_user_id=42,
            agent=_roster_agent(),
        )


def _revoke_buttons(view: CodingKeysView) -> list[discord.ui.Button[discord.ui.LayoutView]]:
    return [i for i in _walk(view) if isinstance(i, discord.ui.Button) and i.label == "Revoke"]


def _text(view: CodingKeysView) -> str:
    return "\n".join(str(i.content) for i in _walk(view) if isinstance(i, discord.ui.TextDisplay))


@pytest.fixture
def world(db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]) -> _World:
    return _World(db_session, db_session_factory)


async def test_an_owner_sees_only_their_own_keys_for_this_agent(world: _World) -> None:
    await world.build()

    view = await world.view(as_account=world.owner.id, interaction=_member_interaction())

    assert view is not None
    assert {e.jti for e in view.entries} == {world.owner_key, world.owner_key_2}
    assert len(_revoke_buttons(view)) == 2
    text = _text(view)
    assert "Your live key" in text and "(you)" in text
    assert "eyJ" not in text, "a listing never carries a token"


async def test_an_admin_sees_every_key_for_this_agent_and_none_of_another(world: _World) -> None:
    await world.build()

    view = await world.view(as_account=world.admin.id, interaction=_admin_interaction())

    assert view is not None
    assert {e.jti for e in view.entries} == {
        world.owner_key,
        world.owner_key_2,
        world.admin_key,
    }
    assert world.elsewhere_key not in {e.jti for e in view.entries}


async def test_another_member_cannot_open_the_list(world: _World) -> None:
    await world.build()

    assert await world.view(as_account=world.member.id, interaction=_member_interaction()) is None


async def test_a_member_pressing_the_keys_button_gets_the_refusal(
    world: _World, db_session: AsyncSession
) -> None:
    await world.build()
    await db_session.commit()
    state = _state(_details(), account_id=world.member.id, is_admin=False)
    state.owned_agent_ids.add(_MA_ID)  # a stale snapshot still showing the button
    view = DetailsView(state, runtime=world.runtime, allowed_user_id=42)
    interaction = _member_interaction()

    await _find_button(view, "🔑 Coding keys").callback(interaction)

    interaction.followup.send.assert_awaited_once_with(CODING_KEYS_REFUSAL, ephemeral=True)


async def test_an_owner_revokes_their_own_key_from_the_list(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    await world.build()
    monkeypatch.setattr(CodingKeysView, "swap_to", AsyncMock())
    view = await world.view(as_account=world.owner.id, interaction=_member_interaction())
    assert view is not None

    await view._on_revoke(  # pyright: ignore[reportPrivateUsage]
        _member_interaction(), jti=world.owner_key
    )

    async with world.runtime.sessionmaker() as session:
        row = await get_mcp_token(session, jti=world.owner_key)
        untouched = await get_mcp_token(session, jti=world.owner_key_2)
    assert row is not None and row.revoked_at is not None
    assert untouched is not None and untouched.revoked_at is None


async def test_an_owner_cannot_revoke_an_admins_key_even_by_naming_it(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    await world.build()
    monkeypatch.setattr(CodingKeysView, "swap_to", AsyncMock())
    view = await world.view(as_account=world.owner.id, interaction=_member_interaction())
    assert view is not None
    interaction = _member_interaction()

    await view._on_revoke(interaction, jti=world.admin_key)  # pyright: ignore[reportPrivateUsage]

    async with world.runtime.sessionmaker() as session:
        row = await get_mcp_token(session, jti=world.admin_key)
    assert row is not None and row.revoked_at is None


async def test_an_admin_revokes_any_key_of_the_agent_but_not_of_another_agent(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    await world.build()
    monkeypatch.setattr(CodingKeysView, "swap_to", AsyncMock())
    view = await world.view(as_account=world.admin.id, interaction=_admin_interaction())
    assert view is not None

    await view._on_revoke(  # pyright: ignore[reportPrivateUsage]
        _admin_interaction(), jti=world.owner_key
    )
    await view._on_revoke(  # pyright: ignore[reportPrivateUsage]
        _admin_interaction(), jti=world.elsewhere_key
    )

    async with world.runtime.sessionmaker() as session:
        owner_row = await get_mcp_token(session, jti=world.owner_key)
        elsewhere_row = await get_mcp_token(session, jti=world.elsewhere_key)
    assert owner_row is not None and owner_row.revoked_at is not None
    assert elsewhere_row is not None and elsewhere_row.revoked_at is None
