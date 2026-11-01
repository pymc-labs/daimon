"""An OIDC provider vouching for chat accounts: one person per workspace, as that account."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from daimon.core import web_identity
from daimon.core.chat_accounts import ChatAccount
from daimon.core.config import WebOidcSettings, WebSettings
from daimon.core.stores import web_audit
from daimon.core.stores import web_chat as chat_store
from daimon.core.stores import web_identities as identities_store
from daimon.core.stores import web_people as people_store
from daimon.core.stores import web_roles as roles_store
from daimon.core.stores import workspace_members as members_store
from daimon.core.stores.domain import WebPersonRow
from daimon.core.stores.identity import find_platform_principal
from daimon.core.web_identity import VerifiedIdentity, resolve_web_people
from daimon.testing.factories import (
    make_account,
    make_platform_principal,
    make_tenant,
    make_web_person,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_ISSUER = "https://id.example.com"
_GUILD = "123456789012345678"
_USER = "223456789012345678"
_NOW = datetime(2026, 5, 1, tzinfo=UTC)


def _settings(home: uuid.UUID, *, trust: bool = True) -> WebSettings:
    return WebSettings(
        public_url="https://web.example.com",  # pyright: ignore[reportArgumentType]  # coerced
        oidc=WebOidcSettings(
            issuer=_ISSUER,
            client_id="web-client",
            client_secret="secret",  # pyright: ignore[reportArgumentType]  # SecretStr coerces
            tenant_id=home,
            trust_chat_accounts=trust,
        ),
    )


def _identity(subject: str = "sub-1") -> VerifiedIdentity:
    return VerifiedIdentity(
        provider="oidc",
        issuer=_ISSUER,
        subject=subject,
        display_name="Ada",
        email="ada@example.com",
        email_verified=True,
        groups=("staff",),
        chat_accounts=(ChatAccount("discord", _USER, None),),
    )


async def _guild_listing_the_user(db_session: AsyncSession) -> uuid.UUID:
    guild = await make_tenant(db_session, platform="discord", workspace_id=_GUILD)
    await members_store.replace_members(
        db_session, tenant_id=guild.id, members={_USER: False}, workspace_name="Guild", now=_NOW
    )
    return guild.id


async def test_vouched_account_adds_its_workspace_and_keeps_the_home_one(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    home = await make_tenant(db_session, platform="web", workspace_id="home")
    guild_id = await _guild_listing_the_user(db_session)
    await db_session.commit()

    people = await resolve_web_people(
        db_session_factory, identity=_identity(), settings=_settings(home.id), clock=lambda: _NOW
    )

    by_tenant = {p.tenant_id: p for p in people}
    assert set(by_tenant) == {guild_id, home.id}, "the guild the account is in, and home"
    async with db_session_factory() as session:
        principal = await find_platform_principal(
            session, tenant_id=guild_id, platform="discord", external_id=_USER
        )
        linked = await identities_store.list_for_person(session, person_id=by_tenant[guild_id].id)
        audit = await web_audit.list_recent(session, tenant_id=guild_id)
    assert principal is not None and by_tenant[guild_id].account_id == principal.account_id, (
        "in the guild the person acts as the vouched Discord account"
    )
    assert [(i.provider, i.groups) for i in linked] == [("oidc", ("staff",))], (
        "the OIDC login and its groups are linked, so group roles apply"
    )
    assert [a.action for a in audit] == ["Signed in with vouched chat account"]


async def test_vouching_is_ignored_unless_trusted(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    home = await make_tenant(db_session, platform="web", workspace_id="home")
    await _guild_listing_the_user(db_session)
    await db_session.commit()

    people = await resolve_web_people(
        db_session_factory, identity=_identity(), settings=_settings(home.id, trust=False)
    )

    assert [p.tenant_id for p in people] == [home.id], "an untrusted claim reaches nothing"


async def test_an_earlier_oidc_person_moves_onto_the_chat_account_with_role_and_chats(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    guild_id = await _guild_listing_the_user(db_session)
    await db_session.commit()
    untrusted = _settings(guild_id, trust=False)
    (earlier,) = await resolve_web_people(
        db_session_factory, identity=_identity(), settings=untrusted
    )
    async with db_session_factory() as session, session.begin():
        await roles_store.set_workspace_role(
            session,
            tenant_id=guild_id,
            account_id=earlier.account_id,
            role="admin",
            granted_by_account_id=None,
        )
        thread = await chat_store.create_thread(
            session,
            tenant_id=guild_id,
            person_id=earlier.id,
            agent_id="a",
            agent_name="A",
            title="t",
        )

    (moved,) = await resolve_web_people(
        db_session_factory, identity=_identity(), settings=_settings(guild_id)
    )

    assert moved.id == earlier.id and moved.account_id != earlier.account_id, (
        "the same person row now runs as the chat account"
    )
    async with db_session_factory() as session:
        role = await roles_store.get_workspace_role(
            session, tenant_id=guild_id, account_id=moved.account_id
        )
        kept = await chat_store.get_thread(session, thread_id=thread.id, person_id=moved.id)
    assert role == "admin", "the role moves with the person"
    assert kept is not None, "the chats stay with the person"


async def test_a_chat_person_already_there_takes_the_login_and_the_chats(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    guild = await make_tenant(db_session, platform="discord", workspace_id=_GUILD)
    await members_store.replace_members(
        db_session, tenant_id=guild.id, members={_USER: False}, workspace_name="Guild", now=_NOW
    )
    guild_id = guild.id
    home = await make_tenant(db_session, platform="web", workspace_id="home")
    account = await make_account(db_session, tenant=guild)
    await make_platform_principal(
        db_session, platform="discord", external_id=_USER, tenant=guild, account=account
    )
    chat_person = await make_web_person(db_session, tenant=guild, account=account)
    await db_session.commit()
    (earlier,) = await resolve_web_people(
        db_session_factory, identity=_identity(), settings=_settings(guild_id, trust=False)
    )
    async with db_session_factory() as session, session.begin():
        thread = await chat_store.create_thread(
            session,
            tenant_id=guild_id,
            person_id=earlier.id,
            agent_id="a",
            agent_name="A",
            title="t",
        )

    people = await resolve_web_people(
        db_session_factory, identity=_identity(), settings=_settings(home.id)
    )

    by_tenant = {p.tenant_id: p for p in people}
    assert by_tenant[guild_id].id == chat_person.id, "the chat person wins"
    async with db_session_factory() as session:
        moved = await chat_store.get_thread(session, thread_id=thread.id, person_id=chat_person.id)
        left = await identities_store.list_for_person(session, person_id=earlier.id)
    assert moved is not None, "the chats move over"
    assert left == [], "the login leaves the earlier person"


async def test_a_chat_person_signing_in_with_another_provider_account_is_not_merged(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Two provider accounts claiming one chat account: the first keeps it, the second is
    refused there and the refusal is audited with both."""
    home = await make_tenant(db_session, platform="web", workspace_id="home")
    guild_id = await _guild_listing_the_user(db_session)
    await db_session.commit()
    settings = _settings(home.id)
    first = await resolve_web_people(
        db_session_factory, identity=_identity("sub-0"), settings=settings, clock=lambda: _NOW
    )

    second = await resolve_web_people(
        db_session_factory, identity=_identity("sub-1"), settings=settings, clock=lambda: _NOW
    )

    chat_person = next(p for p in first if p.tenant_id == guild_id)
    assert [p.tenant_id for p in second] == [home.id], "the guild is refused, home still works"
    async with db_session_factory() as session:
        linked = await identities_store.list_for_person(session, person_id=chat_person.id)
        audit = await web_audit.list_recent(session, tenant_id=guild_id)
    assert [i.subject for i in linked] == ["sub-0"], "the chat person keeps only its own login"
    refused = [a for a in audit if a.action == "Refused vouched chat account"]
    assert len(refused) == 1 and refused[0].target_person_id == chat_person.id, audit
    assert refused[0].detail is not None and "sub-0" in refused[0].detail, refused[0].detail
    assert "sub-1" in refused[0].detail, "both provider accounts are named"


async def test_the_vouched_sign_in_is_audited_on_first_link_only(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    home = await make_tenant(db_session, platform="web", workspace_id="home")
    guild_id = await _guild_listing_the_user(db_session)
    await db_session.commit()

    for _ in range(3):
        await resolve_web_people(
            db_session_factory, identity=_identity(), settings=_settings(home.id)
        )

    async with db_session_factory() as session:
        audit = await web_audit.list_recent(session, tenant_id=guild_id)
    assert [a.action for a in audit] == ["Signed in with vouched chat account"], (
        "later sign-ins through the same link add nothing"
    )


async def test_a_chat_sign_in_racing_the_move_is_merged_into(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A chat sign-in creates the chat person after the vouched sign-in looked for one and
    before it moves the earlier person onto the account: the move gives way to a merge
    instead of failing on the unique (workspace, account) row."""
    guild_id = await _guild_listing_the_user(db_session)
    await db_session.commit()
    (earlier,) = await resolve_web_people(
        db_session_factory, identity=_identity(), settings=_settings(guild_id, trust=False)
    )
    real = people_store.get_person_for_account
    raced: list[uuid.UUID] = []

    async def read_then_race(
        session: AsyncSession, *, tenant_id: uuid.UUID, account_id: uuid.UUID
    ) -> WebPersonRow | None:
        found = await real(session, tenant_id=tenant_id, account_id=account_id)
        if not raced:
            # What the racing chat sign-in commits right after this read.
            racer = await people_store.upsert_member(
                session,
                tenant_id=tenant_id,
                account_id=account_id,
                display_name="Chat",
                workspace_name="Guild",
                email=None,
                new_person_id=uuid.uuid4(),
            )
            raced.append(racer.id)
        return found

    monkeypatch.setattr(web_identity.people_store, "get_person_for_account", read_then_race)

    (person,) = await resolve_web_people(
        db_session_factory, identity=_identity(), settings=_settings(guild_id)
    )

    assert person.id == raced[0], "the chat person that won the race takes the login"
    async with db_session_factory() as session:
        left = await identities_store.list_for_person(session, person_id=earlier.id)
    assert left == [], "the login leaves the earlier person"
