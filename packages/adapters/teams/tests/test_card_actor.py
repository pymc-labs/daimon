"""card_actor: a clicker from another organisation is treated as anyone, except in a 1:1."""

from __future__ import annotations

import uuid
from typing import Any, cast

import pytest
from daimon.adapters.teams.card_actions import card_actor
from daimon.adapters.teams.runtime import TeamsRuntime
from microsoft_teams.api import InvokeActivity
from pydantic import TypeAdapter
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .conftest import (
    AAD_OBJECT_ID,
    CHANNEL_ID,
    ENTRA_TENANT_ID,
    THREAD_ID,
    build_teams_runtime,
    make_invoke,
)

pytestmark = pytest.mark.usefixtures("provisioned_tenant")
OTHER_TENANT = str(uuid.UUID(int=99))


def _runtime(db: async_sessionmaker[AsyncSession]) -> TeamsRuntime:
    return build_teams_runtime(db, admins=(AAD_OBJECT_ID,))


def _click(*, channel: bool = True, tenant: str = ENTRA_TENANT_ID) -> InvokeActivity:
    action = {"type": "Action.Execute", "verb": "x", "data": {"action": "x"}}
    value = {"action": action, "trigger": "manual"}
    payload = make_invoke("adaptiveCard/action", value, chat=THREAD_ID if channel else "a:chat")
    if channel:
        cast(dict[str, Any], payload["conversation"])["conversationType"] = "channel"
    payload["channelData"] = {"tenant": {"id": tenant}, "channel": {"id": CHANNEL_ID}}
    return TypeAdapter[InvokeActivity](InvokeActivity).validate_python(payload)


async def test_a_foreign_clicker_in_a_channel_is_answered_like_anyone(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    actor = await card_actor(_runtime(db_session_factory), _click(tenant=OTHER_TENANT))
    assert actor is not None and actor.user_id == AAD_OBJECT_ID, "a shared channel may hold them"


async def test_a_foreign_clicker_in_a_1_1_chat_is_refused(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    click = _click(channel=False, tenant=OTHER_TENANT)
    assert await card_actor(_runtime(db_session_factory), click) is None, (
        "Teams opens no 1:1 chat across organisations"
    )


async def test_our_own_admin_clicking_is_an_admin(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    runtime = _runtime(db_session_factory)
    for click in (_click(), _click(channel=False)):
        actor = await card_actor(runtime, click)
        assert actor is not None and actor.is_admin, "the admin list decides, wherever they click"


async def test_a_group_chat_click_is_answered(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    action = {"type": "Action.Execute", "verb": "x", "data": {"action": "x"}}
    payload = make_invoke("adaptiveCard/action", {"action": action}, chat="19:g@thread.v2")
    cast(dict[str, Any], payload["conversation"])["conversationType"] = "groupChat"
    payload["channelData"] = {"tenant": {"id": OTHER_TENANT}}
    click = TypeAdapter[InvokeActivity](InvokeActivity).validate_python(payload)
    assert await card_actor(_runtime(db_session_factory), click) is not None, (
        "a group chat may hold other organisations"
    )


async def test_granted_clicker_gets_setup_authority_but_no_billing_authority(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from daimon.core.ma_identity import derive_tenant_uuid
    from daimon.core.stores.daimon_admins import change_grant

    runtime = build_teams_runtime(db_session_factory, admins=())
    tenant_id = derive_tenant_uuid(platform="teams", workspace_id=ENTRA_TENANT_ID)
    async with db_session_factory.begin() as session:
        await change_grant(
            session,
            tenant_id=tenant_id,
            platform="teams",
            subject_kind="user",
            subject_id=AAD_OBJECT_ID,
            actor_user_id="owner",
            is_platform_admin=True,
        )
    actor = await card_actor(runtime, _click())
    assert actor is not None and actor.is_admin and not actor.is_platform_admin
    async with db_session_factory.begin() as session:
        await change_grant(
            session,
            tenant_id=tenant_id,
            platform="teams",
            subject_kind="user",
            subject_id=AAD_OBJECT_ID,
            actor_user_id="owner",
            is_platform_admin=True,
            revoke=True,
        )
    actor = await card_actor(runtime, _click())
    assert actor is not None and not actor.is_admin
