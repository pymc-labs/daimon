"""The chat-to-web link builder: encoding, role and module limits, and `web_awareness`."""

from __future__ import annotations

import uuid
from typing import cast

from anthropic import AsyncAnthropic
from daimon.core.config import DatabaseSettings, Settings, WebSettings
from daimon.core.features.snapshot import FeaturePlace
from daimon.core.ma_resolver import new_resolver_cache
from daimon.core.routine_delivery import routine_links
from daimon.core.scope import ChannelScopeRef, DeploymentDefault
from daimon.core.stores.domain import FeatureTarget
from daimon.core.stores.feature_values import set_feature_value
from daimon.core.stores.scoped_config_write import set_fields
from daimon.core.web_links import Assignment, TenantLinks, WebLinks, links_at
from daimon.testing.factories import make_routine, make_tenant, make_tenant_config
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

TENANT = uuid.UUID("11111111-2222-3333-4444-555555555555")


def _links(modules: str | None = None) -> WebLinks:
    extra = {} if modules is None else {"modules": modules}
    web = WebSettings(public_url="https://app.example.com/", **extra)  # type: ignore[arg-type]
    built = WebLinks.from_settings(web)
    assert built is not None, "a web block yields a builder"
    return built


def test_links_have_no_builder_when_there_is_no_web_block() -> None:
    assert WebLinks.from_settings(None) is None, "self-hosters without a web app get no links"


def test_agent_link_carries_workspace_and_node() -> None:
    tenant = _links().for_tenant(TENANT, admin=False)
    assert tenant.agent("ag_1") == f"https://app.example.com/w/{TENANT}/agents/ag_1"
    assert tenant.agent("ag_1", "skill:s 1") == (
        f"https://app.example.com/w/{TENANT}/agents/ag_1?node=skill%3As%201"
    ), "the node is encoded"


def test_channel_link_encodes_a_teams_channel_id() -> None:
    url = _links().for_tenant(TENANT, admin=False).channel("19:abc@thread.tacv2")
    assert url == f"https://app.example.com/w/{TENANT}/channels?channel=19%3Aabc%40thread.tacv2"


def test_a_member_gets_no_link_to_an_admin_page() -> None:
    member = _links().for_tenant(TENANT, admin=False)
    admin = _links().for_tenant(TENANT, admin=True)
    assert member.person("p1") is None, "people management is admin-only"
    assert member.skill("s1") is None, "the skill library needs workspace management"
    assert member.allowance() is None
    assert admin.person("p1") is not None
    assert admin.skill("s1") is not None
    assert member.routine(TENANT) is not None, "members manage routines"
    assert member.spend() is not None, "members see their own spend"


def test_a_page_of_a_module_that_is_off_has_no_link() -> None:
    tenant = _links("chat,agents").for_tenant(TENANT, admin=True)
    assert tenant.agent("ag_1") is not None
    assert tenant.routine(TENANT) is None, "routines module is off"
    assert tenant.channel("c1") is None, "channels module is off"


def test_pages_are_the_list_pages_the_caller_may_open() -> None:
    """Pages are paths under the workspace root, so the id is spelled once per turn."""
    member = _links().for_tenant(TENANT, admin=False).pages()
    assert member == {
        "chat": "/chat",
        "agents": "/agents",
        "channels": "/channels",
        "routines": "/routines",
        "spend": "/credits/spend",
        "profile": "/profile",
    }, "a member has no integrations or admin pages"
    admin = _links().for_tenant(TENANT, admin=True).pages()
    assert admin["integrations"] == "/integrations", "an admin has the integrations list"
    assert admin["github"] == "/integrations/github"
    assert admin["admin"] == "/admin", "the admin page opens on no one"


def test_pages_leave_out_modules_that_are_off_but_keep_the_profile() -> None:
    pages = _links("chat").for_tenant(TENANT, admin=True).pages()
    assert set(pages) == {"chat", "profile"}, "every other page belongs to a module that is off"


async def test_a_channel_card_follows_the_agent_assigned_there(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """An agent's Off reaches a card in a channel assigned to it before anyone used it there,
    and leaves once the channel is reassigned; a channel on the workspace default keeps it."""
    async with db_session_factory.begin() as db:
        tenant = await make_tenant(db)
        await make_tenant_config(db, tenant=tenant, agent_name="on-agent", mode="agent")
        await _assign(db, tenant.id, "off-agent")
        await set_feature_value(
            db,
            tenant_id=tenant.id,
            target=FeatureTarget("agent", agent_id="agent_off"),
            key="web_awareness",
            value="off",
            set_by=None,
            surface="chat",
        )
    cache = new_resolver_cache()
    cache[(tenant.id, "agent", "off-agent")] = "agent_off"
    cache[(tenant.id, "agent", "on-agent")] = "agent_on"
    assignment = Assignment(DeploymentDefault(), cast(AsyncAnthropic, object()), cache)
    links = _links().for_tenant(tenant.id, admin=False)

    async def at(channel: str) -> TenantLinks | None:
        place = FeaturePlace(tenant.id, "discord", channel)
        return await links_at(db_session_factory, None, place, links, assignment)

    assert [await at("C1"), await at("C2")] == [None, links], "Off for C1's agent only"
    async with db_session_factory.begin() as db:
        await _assign(db, tenant.id, "on-agent")
    assert await at("C1") == links, "C1 now assigned to an agent with it On"


async def _assign(db: AsyncSession, tenant_id: uuid.UUID, agent_name: str) -> None:
    scope = ChannelScopeRef(tenant_id=tenant_id, channel_id="C1")
    await set_fields(db, scope=scope, tenant_id=tenant_id, agent_name=agent_name, mode="agent")


async def test_a_routine_fire_has_no_links_in_a_thread_with_the_web_app_off(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with db_session_factory.begin() as db:
        tenant = await make_tenant(db)
        row = await make_routine(db, tenant=tenant, channel_id="R1")
        await set_feature_value(
            db,
            tenant_id=tenant.id,
            target=FeatureTarget("thread", channel_id="R1", thread_key="T1"),
            key="web_awareness",
            value="off",
            set_by=None,
            surface="chat",
        )
    settings = Settings(
        database=DatabaseSettings(url="postgresql+asyncpg://unused/db"),  # type: ignore[arg-type]
        web=WebSettings(public_url="https://app.example.com/"),  # type: ignore[arg-type]
    )
    into = row.model_copy(update={"destination_kind": "thread", "destination_id": "T1"})
    assert await routine_links(db_session_factory, settings, row, "discord") is not None
    assert await routine_links(db_session_factory, settings, into, "discord") is None, (
        "a fire into the Off thread has no links"
    )
