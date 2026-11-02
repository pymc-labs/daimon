"""The workspace access settings: who may change them, and what each write does."""

from __future__ import annotations

import pytest
from daimon.core import workspace_settings as ws
from daimon.core.access_policy import OPEN_ACCESS_POLICY
from daimon.core.authz import Action, Subject, authorize
from daimon.core.workspace_settings import SettingRefused
from daimon.testing.factories import make_tenant
from sqlalchemy.ext.asyncio import AsyncSession

ADMIN = Subject(is_admin=True, platform_user_id="1" * 17)
MEMBER = Subject(platform_user_id="2" * 17)
DISCORD_ID = "123456789012345678"


def test_only_a_trusted_admin_may_change_access_settings() -> None:
    def may(subject: Subject) -> bool:
        return authorize(
            OPEN_ACCESS_POLICY, subject=subject, action=Action.SET_WORKSPACE_SETTINGS
        ).allowed

    assert may(ADMIN)
    assert not may(MEMBER)
    assert not may(Subject(is_admin=True, via_agent_key=True))
    assert not may(Subject(is_admin=True, client=True))


@pytest.mark.parametrize(
    ("platform", "good", "bad"),
    [
        ("discord", DISCORD_ID, "abc"),
        ("slack", "U01ABCDEF", "u01"),
        ("teams", "0b9d3c6e-1f2a-4b7c-8d9e-0a1b2c3d4e5f", "not-a-guid"),
    ],
)
def test_invokers_must_look_like_ids_of_the_platform(platform: str, good: str, bad: str) -> None:
    assert ws.parse_invokers([f" {good} ", good], platform=platform) == (good,)
    with pytest.raises(SettingRefused, match="not a valid"):
        ws.parse_invokers([bad], platform=platform)


def test_trusted_ids_skip_the_pattern_but_blanks_never_pass() -> None:
    person = "7a1f6a54-2b0e-4a76-9a3b-1c5d6e7f8a9b"
    assert ws.parse_invokers([person], platform="discord", trusted={person}) == (person,)
    with pytest.raises(SettingRefused):
        ws.parse_invokers(["  "], platform="discord", trusted={"  "})


async def test_invokers_round_trip_and_an_empty_list_opens_the_workspace(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)
    stored = await ws.set_invokers(
        db_session, tenant_id=tenant.id, subject=ADMIN, platform="discord", user_ids=[DISCORD_ID]
    )
    assert stored == (DISCORD_ID,)
    assert (await ws.load_access(db_session, tenant_id=tenant.id)).invoker_user_ids == (DISCORD_ID,)

    stored = await ws.set_invokers(
        db_session, tenant_id=tenant.id, subject=ADMIN, platform="discord", user_ids=[]
    )
    assert stored == ()


async def test_a_member_cannot_change_any_setting(db_session: AsyncSession) -> None:
    tenant = await make_tenant(db_session)
    with pytest.raises(SettingRefused, match="admin"):
        await ws.set_invokers(
            db_session, tenant_id=tenant.id, subject=MEMBER, platform="discord", user_ids=[]
        )
    with pytest.raises(SettingRefused, match="admin"):
        await ws.set_dm_memory_read_only(
            db_session, tenant_id=tenant.id, subject=MEMBER, enabled=True
        )
    with pytest.raises(SettingRefused, match="admin"):
        await ws.set_dms(
            db_session, tenant_id=tenant.id, subject=MEMBER, platform="slack", enabled=True
        )
    with pytest.raises(SettingRefused, match="admin"):
        await ws.set_plain_text_mentions(
            db_session, tenant_id=tenant.id, subject=MEMBER, platform="slack", enabled=True
        )
    access = await ws.load_access(db_session, tenant_id=tenant.id)
    assert (access.dm_enabled, access.dm_memory_read_only, access.plain_text_mentions) == (
        False,
        False,
        False,
    )


async def test_dms_memory_and_mentions_each_change_alone(db_session: AsyncSession) -> None:
    tenant = await make_tenant(db_session)
    await ws.set_dms(
        db_session, tenant_id=tenant.id, subject=ADMIN, platform="discord", enabled=True
    )
    assert await ws.set_dm_memory_read_only(
        db_session, tenant_id=tenant.id, subject=ADMIN, enabled=True
    )
    await ws.set_plain_text_mentions(
        db_session, tenant_id=tenant.id, subject=ADMIN, platform="teams", enabled=True
    )
    access = await ws.load_access(db_session, tenant_id=tenant.id)
    assert access == ws.WorkspaceAccess(
        invoker_user_ids=(), dm_enabled=True, dm_memory_read_only=True, plain_text_mentions=True
    )


async def test_settings_a_platform_lacks_are_refused(db_session: AsyncSession) -> None:
    tenant = await make_tenant(db_session)
    with pytest.raises(SettingRefused, match="Private chats"):
        await ws.set_dms(
            db_session, tenant_id=tenant.id, subject=ADMIN, platform="teams", enabled=True
        )
    with pytest.raises(SettingRefused, match="Typed mentions"):
        await ws.set_plain_text_mentions(
            db_session, tenant_id=tenant.id, subject=ADMIN, platform="web", enabled=True
        )
