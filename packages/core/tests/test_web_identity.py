"""Mapping a verified Discord sign-in to the people it acts as."""

from __future__ import annotations

from dataclasses import replace

import pytest
from daimon.core.stores import web_identities as identities_store
from daimon.core.stores.domain import Role
from daimon.core.turn_hold import RequesterOrg
from daimon.core.web_identity import VerifiedIdentity, WebPrincipal, resolve_web_people
from daimon.testing.factories import make_tenant
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


def _identity(*workspaces: tuple[str, str]) -> VerifiedIdentity:
    return VerifiedIdentity(
        provider="discord", subject="u-1", display_name="Ada", workspaces=workspaces
    )


async def test_sign_in_makes_a_member_on_the_chat_principal_of_each_reached_workspace(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant = await make_tenant(db_session, platform="discord", workspace_id="g-1")
    await make_tenant(db_session, platform="discord", workspace_id="g-2")
    await db_session.commit()

    (person,) = await resolve_web_people(db_session_factory, identity=_identity(("g-1", "Acme")))

    assert (person.tenant_id, person.kind, person.workspace_name) == (
        tenant.id,
        "member",
        "Acme",
    ), "only the proven workspace is reached, named as the sign-in names it"
    async with db_session_factory() as session:
        stored = await identities_store.list_for_person(session, person_id=person.id)
    assert [(i.provider, i.issuer, i.subject) for i in stored] == [("discord", "", "u-1")], (
        "the Discord identity is kept with no issuer"
    )


async def test_sign_in_stores_the_email_it_carries_and_keeps_it_when_a_later_one_has_none(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    await make_tenant(db_session, platform="teams", workspace_id="dir-1")
    await db_session.commit()
    with_email = replace(_identity(("dir-1", "Acme")), provider="microsoft", email="ada@acme.test")

    (first,) = await resolve_web_people(db_session_factory, identity=with_email)
    (again,) = await resolve_web_people(
        db_session_factory, identity=replace(with_email, email=None)
    )

    assert first.email == "ada@acme.test", "the sign-in's email lands on the person"
    assert again.email == "ada@acme.test", "a sign-in without one keeps the stored email"


@pytest.mark.parametrize(
    ("org", "client"),
    [
        (RequesterOrg.HOST, False),
        (RequesterOrg.OTHER, True),
        (RequesterOrg.OUTSIDER, True),
        (RequesterOrg.UNKNOWN, True),
    ],
)
def test_only_a_host_member_is_not_a_client_on_the_web(org: RequesterOrg, client: bool) -> None:
    principal = WebPrincipal(
        platform="discord", external_id="u-1", role=Role.USER, org=org, home_id=None
    )
    assert principal.subject.client is client


@pytest.mark.parametrize(
    ("mirror_roles", "mode", "org"),
    [
        (["9002"], "on", RequesterOrg.OUTSIDER),
        (None, "on", RequesterOrg.OUTSIDER),
        (["9001"], "on", RequesterOrg.HOST),
        (["9002"], "shadow", RequesterOrg.HOST),
        (["9002"], "off", RequesterOrg.HOST),
    ],
)
async def test_a_linked_member_with_no_internal_role_is_an_outsider_on_the_web_in_mode_on(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    mirror_roles: list[str] | None,
    mode: str,
    org: RequesterOrg,
) -> None:
    """The hub treats an outsider as a client, so every private surface refuses them."""
    from datetime import UTC, datetime

    from daimon.core.access_policy import TenantAccessPolicy
    from daimon.core.authz import Action, Surface, authorize
    from daimon.core.stores.access_policy import set_access_policy
    from daimon.core.stores.workspace_members import upsert_member
    from daimon.core.web_identity import principal_of
    from daimon.testing.factories import make_account, make_platform_principal, make_web_person

    async def fake_mode(*_a: object, **_k: object) -> str:
        return mode

    monkeypatch.setattr("daimon.core.channel_audience.audience_mode", fake_mode)
    monkeypatch.setattr("daimon.core.web_identity.audience_mode", fake_mode)
    tenant = await make_tenant(db_session, platform="discord", workspace_id="g-web")
    account = await make_account(db_session, tenant=tenant)
    await make_platform_principal(
        db_session, platform="discord", external_id="8123", tenant=tenant, account=account
    )
    person = await make_web_person(db_session, tenant=tenant, account=account)
    await set_access_policy(
        db_session, tenant_id=tenant.id, policy=TenantAccessPolicy(internal_role_ids=("9001",))
    )
    await upsert_member(
        db_session,
        tenant_id=tenant.id,
        platform_user_id="8123",
        is_admin=False,
        now=datetime.now(UTC),
        role_ids=mirror_roles,
    )

    principal = await principal_of(db_session, person, "member")

    assert principal.org is org
    if org is RequesterOrg.OUTSIDER:
        assert principal.subject.client
        for surface in (Surface.HUB, Surface.AGENT_CHAT):
            assert not authorize(
                TenantAccessPolicy(),
                subject=principal.subject,
                action=Action.START_TURN,
                surface=surface,
            )


@pytest.mark.parametrize(
    ("mirror_roles", "runs"),
    [(["9001"], True), (["9002"], False), (None, False), ("unlisted", False)],
)
async def test_the_web_chat_runs_a_role_gated_agent_for_a_linked_holder_of_its_role(
    db_session: AsyncSession, mirror_roles: list[str] | str | None, runs: bool
) -> None:
    """The linked Discord account's roles come from the member mirror; unknown refuses."""
    from datetime import UTC, datetime

    from daimon.core.access_policy import AgentRule, TenantAccessPolicy
    from daimon.core.authz import Action, AgentRef, Place, Surface, authorize
    from daimon.core.stores.workspace_members import upsert_member
    from daimon.core.web_identity import principal_of
    from daimon.testing.factories import make_account, make_platform_principal, make_web_person

    tenant = await make_tenant(db_session, platform="discord", workspace_id="g-roles")
    account = await make_account(db_session, tenant=tenant)
    await make_platform_principal(
        db_session, platform="discord", external_id="8123", tenant=tenant, account=account
    )
    person = await make_web_person(db_session, tenant=tenant, account=account)
    if not isinstance(mirror_roles, str):
        await upsert_member(
            db_session,
            tenant_id=tenant.id,
            platform_user_id="8123",
            is_admin=False,
            now=datetime.now(UTC),
            role_ids=mirror_roles,
        )

    principal = await principal_of(db_session, person, "admin")

    gated = TenantAccessPolicy(agent_rules={"helper": AgentRule(invoker_role_ids=("9001",))})
    decision = authorize(
        gated,
        subject=principal.subject,
        action=Action.RUN_AGENT,
        surface=Surface.WEB,
        agent=AgentRef.of("helper"),
        place=Place(channel_id="web:p1", parent_channel_id="web:p1"),
    )
    assert bool(decision) is runs, "an admin needs the role too"
