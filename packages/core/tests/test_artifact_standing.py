"""An outsider (a Discord member with no internal role while `audience_hold` is on) counts as
a visitor for pages, in every core rule that asks."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from cryptography.fernet import Fernet
from daimon.core.artifact_pages.access import PageChange
from daimon.core.artifact_pages.model import ArtifactError
from daimon.core.artifact_pages.standing import outsider_accounts, visitor_accounts
from daimon.core.config import ArtifactPagesSettings
from daimon.core.stores import artifact_pages as store
from daimon.core.stores import web_people
from daimon.core.stores.domain import AccountRow, ArtifactRow, TenantRow
from daimon.core.turn_hold import RequesterOrg
from daimon.core.web_identity import principal_of
from daimon.testing.factories import make_account, make_tenant, make_web_person
from daimon.testing.outsiders import hold_audience, link_discord_member
from sqlalchemy.ext.asyncio import AsyncSession

NOW = datetime(2026, 11, 22, 12, tzinfo=UTC)


async def _publish(
    db: AsyncSession, tenant: TenantRow, owner: AccountRow, change: PageChange, slug: str = "plan"
) -> ArtifactRow:
    return await store.publish(
        db,
        tenant_id=tenant.id,
        agent_id="ag_1",
        account_id=owner.id,
        is_admin=True,
        slug=slug,
        title="Plan",
        html="<p>v</p>",
        change=change,
        origin=None,
        new_link=False,
        now=NOW,
        limits=ArtifactPagesSettings(),
    )


class Server:
    """A Discord workspace with audience_hold on: an owner, an insider, an outsider, a
    web visitor, and a member who only ever signed in on the web."""

    def __init__(self, tenant: TenantRow, **accounts: AccountRow) -> None:
        self.tenant = tenant
        self.owner, self.insider, self.outsider, self.visitor, self.web_only = (
            accounts[k] for k in ("owner", "insider", "outsider", "visitor", "web_only")
        )


@pytest.fixture
async def server(db_session: AsyncSession) -> Server:
    db_session.info["crypto_keys"] = (Fernet.generate_key().decode(),)
    tenant = await make_tenant(db_session, platform="discord", workspace_id=f"g-{uuid.uuid4().hex}")
    accounts = {
        name: await make_account(db_session, tenant=tenant)
        for name in ("owner", "insider", "outsider", "visitor", "web_only")
    }
    for account in accounts.values():
        person = await make_web_person(db_session, tenant=tenant, account=account)
        if account is accounts["visitor"]:
            await web_people.update_person(db_session, person_id=person.id, kind="visitor")
    await hold_audience(db_session, tenant.id)
    for name, internal in (("owner", True), ("insider", True), ("outsider", False)):
        await link_discord_member(
            db_session, tenant_id=tenant.id, account_id=accounts[name].id, internal=internal
        )
    return Server(tenant, **accounts)


async def test_visitors_are_web_visitors_and_outsiders_and_no_one_else(
    db_session: AsyncSession, server: Server
) -> None:
    everyone = [a.id for a in (server.owner, server.insider, server.outsider, server.visitor)]
    everyone.append(server.web_only.id)
    found = await visitor_accounts(db_session, tenant_id=server.tenant.id, account_ids=everyone)
    assert found == {server.outsider.id, server.visitor.id}, "no insider, no web-only member"


async def test_the_rule_is_the_web_principals_verdict(
    db_session: AsyncSession, server: Server
) -> None:
    for account in (server.owner, server.insider, server.outsider, server.web_only):
        person = await web_people.get_person_for_account(
            db_session, tenant_id=server.tenant.id, account_id=account.id
        )
        assert person is not None
        principal = await principal_of(db_session, person, "member")
        outsiders = await outsider_accounts(
            db_session, tenant_id=server.tenant.id, account_ids=[account.id]
        )
        assert (account.id in outsiders) is (principal.org is RequesterOrg.OUTSIDER), (
            "one rule for chat and pages"
        )


async def test_a_workspace_without_the_hold_has_no_outsiders(
    db_session: AsyncSession, server: Server, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def off(*_a: object, **_k: object) -> str:
        return "off"

    monkeypatch.setattr("daimon.core.web_identity.audience_mode", off)
    found = await visitor_accounts(
        db_session, tenant_id=server.tenant.id, account_ids=[server.outsider.id]
    )
    assert found == frozenset(), "tenants with no outsider classification are unchanged"


async def test_a_page_naming_an_outsider_is_visible_outside_the_workspace(
    db_session: AsyncSession, server: Server
) -> None:
    named = await _publish(
        db_session,
        server.tenant,
        server.owner,
        PageChange(access="people", people=(server.outsider.id,)),
    )
    inside = await _publish(
        db_session,
        server.tenant,
        server.owner,
        PageChange(access="people", people=(server.insider.id,)),
        slug="inside",
    )
    assert await store.visible_outside_workspace(db_session, named), "they read it as a visitor"
    assert not await store.visible_outside_workspace(db_session, inside)


async def test_tools_may_not_use_a_page_for_an_outsider(
    db_session: AsyncSession, server: Server
) -> None:
    page = await _publish(db_session, server.tenant, server.owner, PageChange())
    roles = {
        name: await store.tool_role(db_session, page, getattr(server, name).id, chat_admin=False)
        for name in ("insider", "outsider", "visitor")
    }
    assert roles == {"insider": "viewer", "outsider": "none", "visitor": "none"}, roles


async def test_an_outsider_is_never_an_editor_or_the_owner(
    db_session: AsyncSession, server: Server
) -> None:
    with pytest.raises(ArtifactError, match="Only members"):
        await _publish(
            db_session, server.tenant, server.owner, PageChange(editors=(server.outsider.id,))
        )
    page = await _publish(
        db_session, server.tenant, server.owner, PageChange(editors=(server.insider.id,))
    )
    with pytest.raises(ArtifactError, match="Only members"):
        await store.update_settings(
            db_session,
            page,
            PageChange(editors=(server.insider.id, server.outsider.id)),
            role="owner",
            account_id=server.owner.id,
            now=NOW,
        )
    await store.update_settings(
        db_session,
        page,
        PageChange(editors=(server.insider.id, server.web_only.id)),
        role="owner",
        account_id=server.owner.id,
        now=NOW,
    )
    # Becoming an outsider after being made an editor, then receiving the page, is refused.
    await link_discord_member(
        db_session,
        tenant_id=server.tenant.id,
        account_id=server.web_only.id,
        internal=False,
    )
    with pytest.raises(ArtifactError, match="Only members"):
        await store.transfer(
            db_session,
            await _refresh(db_session, page),
            to_account_id=server.web_only.id,
            role="owner",
            now=NOW,
        )


async def _refresh(db: AsyncSession, page: ArtifactRow) -> ArtifactRow:
    fresh = await store.get(db, page.id)
    assert fresh is not None
    return fresh
