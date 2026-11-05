"""Mapping a verified Discord sign-in to the people it acts as."""

from __future__ import annotations

from dataclasses import replace

import pytest
from daimon.core.stores import web_identities as identities_store
from daimon.core.web_identity import VerifiedIdentity, WebSignInRefused, resolve_web_people
from daimon.testing.factories import make_tenant
from sqlalchemy import text
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


async def test_sign_in_reaching_no_workspace_is_refused(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    await make_tenant(db_session, platform="discord", workspace_id="g-1")
    await db_session.commit()

    with pytest.raises(WebSignInRefused):
        await resolve_web_people(db_session_factory, identity=_identity(("g-9", "Other")))


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


async def test_a_sign_in_that_only_guesses_the_workspace_name_keeps_the_stored_one(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    await make_tenant(db_session, platform="teams", workspace_id="dir-1")
    await db_session.commit()
    proven = replace(_identity(("dir-1", "Contoso")), provider="microsoft")
    guess = replace(
        _identity(("dir-1", "Microsoft Teams")), provider="microsoft", workspace_names_proven=False
    )

    (kept,) = await resolve_web_people(db_session_factory, identity=proven)
    (later,) = await resolve_web_people(db_session_factory, identity=guess)

    assert (kept.workspace_name, later.workspace_name) == ("Contoso", "Contoso")
    await db_session.execute(text("delete from web_people"))
    await db_session.commit()
    (fresh,) = await resolve_web_people(db_session_factory, identity=guess)
    assert fresh.workspace_name == "Microsoft Teams", "a first-ever row still gets the fallback"
