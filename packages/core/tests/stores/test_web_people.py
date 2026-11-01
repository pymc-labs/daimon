"""Real-Postgres tests for the web people, identities and role stores."""

from __future__ import annotations

import uuid

from daimon.core.stores import web_identities as identities
from daimon.core.stores import web_people as people
from daimon.core.stores import web_roles as roles
from daimon.testing.factories import make_account, make_tenant, make_web_person
from sqlalchemy.ext.asyncio import AsyncSession

_ISSUER = "https://id.example.com"


async def test_upsert_member_keeps_the_first_id_and_refreshes_the_name(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    first = await make_web_person(db_session, tenant=tenant, account=account, display_name="Ada")

    again = await people.upsert_member(
        db_session,
        tenant_id=tenant.id,
        account_id=account.id,
        display_name="Ada L.",
        workspace_name="Renamed",
        email="ada@example.com",
        new_person_id=uuid.uuid4(),
    )

    assert again.id == first.id, "a second sign-in must reuse the person, not mint another"
    assert (again.display_name, again.email) == ("Ada L.", "ada@example.com"), "name is refreshed"
    assert again.kind == "member" and again.status == "active", "new people start as active members"
    assert await people.count_people(db_session, tenant_id=tenant.id) == 1, "one row per account"


async def test_upsert_member_without_an_email_keeps_the_stored_one(
    db_session: AsyncSession,
) -> None:
    """A chat sign-in carries no email; it must not wipe the one an invite or SSO set."""
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    await make_web_person(
        db_session, tenant=tenant, account=account, display_name="Ada", email="ada@example.com"
    )

    again = await people.upsert_member(
        db_session,
        tenant_id=tenant.id,
        account_id=account.id,
        display_name="Ada",
        workspace_name="Acme",
        email=None,
        new_person_id=uuid.uuid4(),
    )

    assert again.email == "ada@example.com", "a missing email must never overwrite the stored one"


async def test_get_person_is_scoped_to_its_workspace(db_session: AsyncSession) -> None:
    tenant = await make_tenant(db_session)
    other = await make_tenant(db_session)
    person = await make_web_person(db_session, tenant=tenant)

    found = await people.get_person(db_session, tenant_id=tenant.id, person_id=person.id)
    leaked = await people.get_person(db_session, tenant_id=other.id, person_id=person.id)

    assert found == person, "the person is found in its own workspace"
    assert leaked is None, "another workspace must not see the person"


async def test_delete_for_account_removes_identities_and_leaves_other_accounts(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)
    gone = await make_web_person(db_session, tenant=tenant)
    kept = await make_web_person(db_session, tenant=tenant)
    for person, subject in ((gone, "a"), (kept, "b")):
        await identities.link_identity(
            db_session,
            tenant_id=tenant.id,
            person_id=person.id,
            provider="oidc",
            issuer=_ISSUER,
            subject=subject,
            label="x",
            groups=["g"],
        )

    deleted = await people.delete_for_account(db_session, account_id=gone.account_id)

    assert deleted == 1, "exactly the one person is deleted"
    assert (
        await identities.find_identity(db_session, provider="oidc", issuer=_ISSUER, subject="a")
        == []
    ), "the identity goes with the person"
    assert (
        len(
            await identities.find_identity(db_session, provider="oidc", issuer=_ISSUER, subject="b")
        )
        == 1
    ), "another account's identity stays"
    assert [p.id for p in await people.list_people(db_session, tenant_id=tenant.id)] == [kept.id]


async def test_find_identity_returns_every_workspace_the_login_reaches(
    db_session: AsyncSession,
) -> None:
    first = await make_web_person(db_session)
    second = await make_web_person(db_session)
    for person in (first, second):
        await identities.link_identity(
            db_session,
            tenant_id=person.tenant_id,
            person_id=person.id,
            provider="oidc",
            issuer=_ISSUER,
            subject="same-login",
            label="Example",
            groups=[],
        )
    await identities.link_identity(
        db_session,
        tenant_id=first.tenant_id,
        person_id=first.id,
        provider="oidc",
        issuer=_ISSUER,
        subject="same-login",
        label="Renamed",
        groups=["staff"],
    )

    found = await identities.find_identity(
        db_session, provider="oidc", issuer=_ISSUER, subject="same-login"
    )

    assert {i.person_id for i in found} == {first.id, second.id}, "one row per workspace"
    refreshed = next(i for i in found if i.person_id == first.id)
    assert (refreshed.label, refreshed.groups) == ("Renamed", ("staff",)), "relink refreshes"
    assert (
        await identities.find_identity(
            db_session, provider="oidc", issuer="https://other.example.com", subject="same-login"
        )
        == []
    ), "the issuer is part of the identity"


async def test_the_workspace_role_is_read_back(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    await roles.set_workspace_role(
        db_session,
        tenant_id=tenant.id,
        account_id=account.id,
        role="owner",
        granted_by_account_id=None,
    )

    assert (
        await roles.get_workspace_role(db_session, tenant_id=tenant.id, account_id=account.id)
        == "owner"
    ), "the workspace grant is read back"


async def test_people_in_a_group_are_counted_once_across_sign_ins(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)
    ann = await make_web_person(db_session, tenant=tenant, email="a@example.com")
    bob = await make_web_person(db_session, tenant=tenant, email="b@example.com")
    for person, subject, groups in (
        (ann, "a1", ["staff"]),
        (ann, "a2", ["staff", "ops"]),
        (bob, "b1", ["ops"]),
    ):
        await identities.link_identity(
            db_session,
            tenant_id=tenant.id,
            person_id=person.id,
            provider="oidc",
            issuer=_ISSUER,
            subject=subject,
            label=subject,
            groups=groups,
        )

    assert (
        await identities.count_people_in_group(db_session, tenant_id=tenant.id, group="staff") == 1
    )
    assert await identities.count_people_in_group(db_session, tenant_id=tenant.id, group="ops") == 2
    assert (
        await identities.count_people_in_group(db_session, tenant_id=tenant.id, group="none") == 0
    )


async def test_every_group_is_listed_with_its_head_count(db_session: AsyncSession) -> None:
    tenant = await make_tenant(db_session)
    ann = await make_web_person(db_session, tenant=tenant, email="a@example.com")
    bob = await make_web_person(db_session, tenant=tenant, email="b@example.com")
    for person, subject, groups in (
        (ann, "a1", ["staff"]),
        (ann, "a2", ["staff", "ops"]),
        (bob, "b1", ["ops"]),
    ):
        await identities.link_identity(
            db_session,
            tenant_id=tenant.id,
            person_id=person.id,
            provider="oidc",
            issuer=_ISSUER,
            subject=subject,
            label=subject,
            groups=groups,
        )

    counts = await identities.count_people_by_group(db_session, tenant_id=tenant.id)

    assert counts == {"staff": 1, "ops": 2}, "each group once, its people counted once"


async def test_people_are_searched_by_name_or_email_ignoring_case_and_wildcards(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)
    ada = await make_web_person(
        db_session, tenant=tenant, display_name="Ada Lovelace", email="ada@example.com"
    )
    await make_web_person(db_session, tenant=tenant, display_name="Bob", email="bob@work.test")

    by_name = await people.list_people(db_session, tenant_id=tenant.id, search="LOVE")
    by_email = await people.list_people(db_session, tenant_id=tenant.id, search="work.test")
    wildcard = await people.list_people(db_session, tenant_id=tenant.id, search="%")

    assert [p.id for p in by_name] == [ada.id], "a name part matches regardless of case"
    assert [p.display_name for p in by_email] == ["Bob"], "an email part matches"
    assert wildcard == [], "a percent sign is text, not a wildcard"
    assert await people.count_members(db_session, tenant_id=tenant.id) == 2, "both are members"


async def test_group_head_counts_leave_out_visitors(db_session: AsyncSession) -> None:
    tenant = await make_tenant(db_session)
    ann = await make_web_person(db_session, tenant=tenant, email="a@example.com")
    vic = await make_web_person(db_session, tenant=tenant, email="v@example.com")
    await people.update_person(db_session, person_id=vic.id, kind="visitor")
    for person, subject in ((ann, "a1"), (vic, "v1")):
        await identities.link_identity(
            db_session,
            tenant_id=tenant.id,
            person_id=person.id,
            provider="oidc",
            issuer=_ISSUER,
            subject=subject,
            label=subject,
            groups=["staff"],
        )

    assert (
        await identities.count_people_in_group(db_session, tenant_id=tenant.id, group="staff") == 1
    )
    assert await identities.count_people_by_group(db_session, tenant_id=tenant.id) == {
        "staff": 1
    }, "a visitor is in no group, so no head count includes them"
