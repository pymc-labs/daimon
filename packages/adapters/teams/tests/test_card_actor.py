"""card_actor: a clicker from another organisation is refused unless the handler opts in."""

from __future__ import annotations

import dataclasses
import uuid
from typing import Any, cast

import pytest
from daimon.adapters.teams.card_actions import card_actor
from daimon.adapters.teams.externals import INTERNAL, Membership
from daimon.adapters.teams.runtime import TeamsRuntime
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.stores.accounts import set_external
from daimon.core.stores.identity import get_or_create_platform_principal
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
    teams_settings,
)

pytestmark = pytest.mark.usefixtures("provisioned_tenant")
OTHER_TENANT = str(uuid.UUID(int=99))
TENANT = derive_tenant_uuid(platform="teams", workspace_id=ENTRA_TENANT_ID)


@dataclasses.dataclass
class _Externals:
    membership: Membership
    asked: list[dict[str, Any]] = dataclasses.field(default_factory=list[dict[str, Any]])

    async def classify(self, **kwargs: Any) -> Membership:
        self.asked.append(kwargs)
        if (home := kwargs["foreign_tenant"]) is not None:
            return Membership(is_external=True, is_known=True, home_tenant_id=home)
        return self.membership


def _runtime(db: async_sessionmaker[AsyncSession], membership: Membership) -> TeamsRuntime:
    runtime = build_teams_runtime(db, teams=teams_settings(admins=(AAD_OBJECT_ID,)))
    return dataclasses.replace(runtime, externals=cast(Any, _Externals(membership)))


def _click(*, channel: bool = True, tenant: str = ENTRA_TENANT_ID) -> InvokeActivity:
    action = {"type": "Action.Execute", "verb": "x", "data": {"action": "x"}}
    value = {"action": action, "trigger": "manual"}
    payload = make_invoke("adaptiveCard/action", value, chat=THREAD_ID if channel else "a:chat")
    if channel:
        cast(dict[str, Any], payload["conversation"])["conversationType"] = "channel"
    payload["channelData"] = {"tenant": {"id": tenant}, "channel": {"id": CHANNEL_ID}}
    return TypeAdapter[InvokeActivity](InvokeActivity).validate_python(payload)


async def test_a_foreign_clicker_in_a_channel_needs_the_opt_in_and_is_never_an_admin(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    runtime = _runtime(db_session_factory, INTERNAL)
    click = _click(tenant=OTHER_TENANT)
    assert await card_actor(runtime, click) is None, "refused by default"
    actor = await card_actor(runtime, click, allow_external=True)
    assert actor is not None
    assert (actor.is_external, actor.home_tenant_id, actor.is_admin) == (True, OTHER_TENANT, False)


async def test_a_foreign_clicker_in_a_1_1_chat_is_refused_even_with_the_opt_in(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    runtime = _runtime(db_session_factory, INTERNAL)
    click = _click(channel=False, tenant=OTHER_TENANT)
    assert await card_actor(runtime, click, allow_external=True) is None, "1:1 stays strict"


async def test_the_roster_marks_a_channel_clicker_the_activity_does_not(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    runtime = _runtime(db_session_factory, Membership(is_external=True, is_known=True))
    assert await card_actor(runtime, _click()) is None, "external per the roster"
    externals = cast(_Externals, runtime.externals)
    assert externals.asked[0]["conversation_id"] == CHANNEL_ID, "the channel, not the thread"


async def test_a_guest_clicking_in_a_1_1_chat_is_refused(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    guest = Membership(is_external=True, is_known=True, is_guest=True)
    runtime = _runtime(db_session_factory, guest)
    assert await card_actor(runtime, _click(channel=False)) is None
    assert cast(_Externals, runtime.externals).asked[0]["kind"] == "dm"


async def test_a_stored_flag_holds_a_clicker_nothing_placed(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    runtime = _runtime(db_session_factory, Membership(is_external=False, is_known=False))
    assert await card_actor(runtime, _click(channel=False)) is not None, "no flag: as today"
    async with db_session_factory.begin() as session:
        principal = await get_or_create_platform_principal(
            session, tenant_id=TENANT, platform="teams", external_id=AAD_OBJECT_ID
        )
        await set_external(session, principal.account_id, True)
    assert await card_actor(runtime, _click(channel=False)) is None, "and never an admin"


async def test_our_own_admin_clicking_is_unchanged(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    runtime = _runtime(db_session_factory, INTERNAL)
    for click in (_click(), _click(channel=False)):
        actor = await card_actor(runtime, click)
        assert actor is not None and actor.is_admin and not actor.is_external
