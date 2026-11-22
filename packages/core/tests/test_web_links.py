"""The chat-to-web link builder: encoding, role and module limits, and `web_awareness`."""

from __future__ import annotations

import uuid

from daimon.core.config import WebSettings
from daimon.core.scope import ChannelScopeRef
from daimon.core.stores.scoped_config_write import set_fields
from daimon.core.web_links import WebLinks
from sqlalchemy.ext.asyncio import AsyncSession

TENANT = uuid.UUID("11111111-2222-3333-4444-555555555555")


def _links(modules: str | None = None) -> WebLinks:
    extra = {} if modules is None else {"modules": modules}
    web = WebSettings(public_url="https://app.example.com/", **extra)  # type: ignore[arg-type]
    built = WebLinks.from_settings(web)
    assert built is not None, "a web block yields a builder"
    return built


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


def test_the_admin_page_is_the_overview_for_admins_only() -> None:
    """The workspace admin page the bot links to is the Overview, the tab Admin opens on."""
    assert _links().for_tenant(TENANT, admin=True).pages()["admin"] == "/admin", "overview"
    assert "admin" not in _links().for_tenant(TENANT, admin=False).pages(), "members get none"


async def _assign(db: AsyncSession, tenant_id: uuid.UUID, agent_name: str) -> None:
    scope = ChannelScopeRef(tenant_id=tenant_id, channel_id="C1")
    await set_fields(db, scope=scope, tenant_id=tenant_id, agent_name=agent_name, mode="agent")
