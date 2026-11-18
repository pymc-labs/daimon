"""The chat-to-web link builder: encoding, role and module limits, and `web_awareness`."""

from __future__ import annotations

import uuid

from daimon.core.config import WebSettings
from daimon.core.features.snapshot import FeaturePlace
from daimon.core.stores.domain import FeatureTarget
from daimon.core.stores.feature_values import set_feature_value
from daimon.core.stores.thread_sessions import create_thread_session
from daimon.core.web_links import WebLinks, links_at
from daimon.testing.factories import make_account, make_tenant
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


async def test_a_channel_card_follows_the_agent_answering_there(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """An agent's Off reaches a card in a channel that agent answers, and only there."""
    async with db_session_factory.begin() as db:
        tenant = await make_tenant(db)
        account = await make_account(db, tenant=tenant)
        await create_thread_session(
            db,
            tenant_id=tenant.id,
            platform="discord",
            thread_id="T1",
            account_id=account.id,
            ma_session_id="sesn_1",
            ma_agent_id="agent_off",
            channel_id="C1",
        )
        await set_feature_value(
            db,
            tenant_id=tenant.id,
            target=FeatureTarget("agent", agent_id="agent_off"),
            key="web_awareness",
            value="off",
            set_by=None,
            surface="chat",
        )
    links = _links().for_tenant(tenant.id, admin=False)
    at = [FeaturePlace(tenant.id, "discord", channel) for channel in ("C1", "C2")]
    found = [await links_at(db_session_factory, None, place, links) for place in at]
    assert found == [None, links], "Off for the agent answering in C1 only, not in C2"
