"""Mapping a verified OIDC sign-in to the person and account it acts as."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from daimon.core.config import WebOidcSettings, WebSettings
from daimon.core.stores import identity as identity_store
from daimon.core.stores import tenants as tenants_store
from daimon.core.stores import web_identities as identities_store
from daimon.core.stores import web_people as people_store
from daimon.core.stores import web_workspace_settings as workspace_settings
from daimon.core.web_identity import VerifiedIdentity, WebSignInRefused, resolve_web_people
from daimon.testing.factories import make_account, make_platform_principal, make_tenant
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_ISSUER = "https://id.example.com"


def _settings(tenant_id: uuid.UUID, *, domains: tuple[str, ...] = ()) -> WebSettings:
    return WebSettings(
        public_url="https://web.example.com",  # pyright: ignore[reportArgumentType]  # coerced
        oidc=WebOidcSettings(
            issuer=_ISSUER,
            client_id="web-client",
            client_secret="secret",  # pyright: ignore[reportArgumentType]  # SecretStr coerces
            tenant_id=tenant_id,
            workspace_name="Example Workspace",
            allowed_domains=domains,
        ),
    )


def _identity(**overrides: object) -> VerifiedIdentity:
    fields: dict[str, object] = {
        "provider": "oidc",
        "issuer": _ISSUER,
        "subject": "user-1",
        "display_name": "Ada",
        "email": "ada@example.com",
        "email_verified": True,
        "groups": ("staff",),
    }
    return VerifiedIdentity(**{**fields, **overrides})  # pyright: ignore[reportArgumentType]


async def test_first_sign_in_creates_a_person_on_its_own_web_principal(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant = await make_tenant(db_session)
    await db_session.commit()

    (person,) = await resolve_web_people(
        db_session_factory, identity=_identity(), settings=_settings(tenant.id)
    )

    assert (person.tenant_id, person.kind, person.status) == (tenant.id, "member", "active")
    assert person.workspace_name == "Example Workspace", "the workspace name comes from settings"
    async with db_session_factory() as session:
        principal = await identity_store.find_platform_principal(
            session, tenant_id=tenant.id, platform="web", external_id=str(person.id)
        )
        stored = await identities_store.list_for_person(session, person_id=person.id)
    assert principal is not None and principal.account_id == person.account_id, (
        "the person's account is the web principal's, keyed by the person id"
    )
    assert [(i.provider, i.subject, i.groups) for i in stored] == [("oidc", "user-1", ("staff",))]


async def test_second_sign_in_reuses_the_person_and_refreshes_name_and_groups(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant = await make_tenant(db_session)
    await db_session.commit()
    settings = _settings(tenant.id)
    (first,) = await resolve_web_people(db_session_factory, identity=_identity(), settings=settings)

    (again,) = await resolve_web_people(
        db_session_factory,
        identity=_identity(display_name="Ada L.", groups=("ops",)),
        settings=settings,
    )

    assert again.id == first.id and again.account_id == first.account_id, (
        "same person, same account"
    )
    assert again.display_name == "Ada L.", "the name follows the provider"
    async with db_session_factory() as session:
        (stored,) = await identities_store.list_for_person(session, person_id=first.id)
        count = await people_store.count_people(session, tenant_id=tenant.id)
    assert stored.groups == ("ops",), "groups are refreshed at each sign-in"
    assert count == 1, "no second person is minted"


async def test_sign_in_is_never_matched_to_a_chat_account_by_email(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant = await make_tenant(db_session)
    chat_account = await make_account(db_session, tenant=tenant)
    await make_platform_principal(
        db_session,
        platform="discord",
        external_id="ada@example.com",
        tenant=tenant,
        account=chat_account,
    )
    await db_session.commit()

    (person,) = await resolve_web_people(
        db_session_factory, identity=_identity(), settings=_settings(tenant.id)
    )

    assert person.account_id != chat_account.id, "an email must never link to a chat account"


@pytest.mark.parametrize(
    ("overrides", "domains"),
    [
        ({"email": "ada@evil.example.net"}, ("example.com",)),
        ({"email_verified": False}, ("example.com",)),
        ({"email": None}, ("example.com",)),
    ],
    ids=["other-domain", "unverified-email", "no-email"],
)
async def test_domain_gate_refuses_what_it_cannot_vouch_for(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    overrides: dict[str, object],
    domains: tuple[str, ...],
) -> None:
    tenant = await make_tenant(db_session)
    await db_session.commit()
    with pytest.raises(WebSignInRefused):
        await resolve_web_people(
            db_session_factory,
            identity=_identity(**overrides),
            settings=_settings(tenant.id, domains=domains),
        )
    async with db_session_factory() as session:
        assert await people_store.count_people(session, tenant_id=tenant.id) == 0, "nothing created"


async def test_domain_gate_admits_a_verified_address_in_an_allowed_domain(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant = await make_tenant(db_session)
    await db_session.commit()
    people = await resolve_web_people(
        db_session_factory,
        identity=_identity(),
        settings=_settings(tenant.id, domains=("example.com",)),
    )
    assert len(people) == 1, "an allowed domain signs in"


async def test_foreign_issuer_other_provider_and_unready_workspace_are_refused(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant = await make_tenant(db_session)
    await db_session.commit()
    settings = _settings(tenant.id)
    with pytest.raises(WebSignInRefused):
        await resolve_web_people(
            db_session_factory,
            identity=_identity(issuer="https://evil.example.com"),
            settings=settings,
        )
    with pytest.raises(WebSignInRefused):
        await resolve_web_people(
            db_session_factory, identity=_identity(provider="discord"), settings=settings
        )
    with pytest.raises(WebSignInRefused, match="isn't ready"):
        await resolve_web_people(
            db_session_factory, identity=_identity(), settings=_settings(uuid.uuid4())
        )
    await tenants_store.set_provision_status(
        db_session_factory, tenant_id=tenant.id, status="pending"
    )
    with pytest.raises(WebSignInRefused, match="isn't ready"):
        await resolve_web_people(db_session_factory, identity=_identity(), settings=settings)


async def test_a_sign_in_method_a_workspace_turned_off_is_refused(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant = await make_tenant(db_session)
    await workspace_settings.save_settings(
        db_session,
        tenant_id=tenant.id,
        modules_off=[],
        signin_off=["oidc"],
        default_agent=None,
        now=datetime.now(UTC),
    )
    await db_session.commit()

    with pytest.raises(WebSignInRefused, match="turned off"):
        await resolve_web_people(
            db_session_factory, identity=_identity(), settings=_settings(tenant.id)
        )
