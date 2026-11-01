"""Who a web person is: a verified sign-in, the people (one per workspace) it acts as,
and the principal their turns, spend and memory run as.

A provider proves an identity (`VerifiedIdentity`); this module maps it to person rows
and the account behind each. OpenID Connect people get a `web` principal of their own
until they link a chat account, and are never matched to one by email: an address the
provider vouches for says nothing about who owns a chat account.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Final, Literal

from daimon.core.authz import Subject
from daimon.core.config import WebOidcSettings, WebProvider, WebSettings
from daimon.core.errors import UserFacingError
from daimon.core.hub_identity import resolve_hub_tenants
from daimon.core.permissions import WEB_PLACE_PREFIX
from daimon.core.spend_gate import SpendPerson, spend_person_of_account
from daimon.core.stores import spend_allowances, usage_events, web_audit
from daimon.core.stores import tenants as tenants_store
from daimon.core.stores import web_chat as chat_store
from daimon.core.stores import web_identities as identities_store
from daimon.core.stores import web_people as people_store
from daimon.core.stores import web_roles as roles_store
from daimon.core.stores import web_workspace_settings as workspace_settings
from daimon.core.stores.domain import Platform, Role, WebPersonRow
from daimon.core.stores.identity import (
    create_platform_principal,
    find_platform_principal,
    get_or_create_platform_principal,
    list_platform_principals_for_account,
)
from daimon.core.turn_hold import RequesterOrg
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


_TURNED_OFF = "This way of signing in is turned off here. Ask an admin."


class WebSignInRefused(UserFacingError):
    """The sign-in is valid but may not enter; the message is safe to show the person."""


@dataclass(frozen=True, slots=True)
class VerifiedIdentity:
    """What a provider proved about the person who just signed in."""

    provider: WebProvider
    issuer: str
    subject: str
    display_name: str
    email: str | None
    email_verified: bool
    groups: tuple[str, ...]
    # (workspace id, workspace name) pairs a chat provider vouches for; empty for OIDC.
    workspaces: tuple[tuple[str, str], ...] = ()
    # The chat platform's own id for the person; None for OIDC.
    platform_user_id: str | None = None


# The platform each chat sign-in proves an id on: Microsoft accounts are Teams people.
PLATFORM_OF: dict[WebProvider, Platform] = {
    "discord": "discord",
    "slack": "slack",
    "microsoft": "teams",
}


def email_domain_allowed(identity: VerifiedIdentity, allowed_domains: tuple[str, ...]) -> bool:
    """Whether the address passes the domain gate. No gate configured: everyone passes.

    Only a verified address counts: an unverified claim could name any domain.
    """
    if not allowed_domains:
        return True
    if not identity.email or not identity.email_verified or "@" not in identity.email:
        return False
    return identity.email.rsplit("@", 1)[1].lower() in allowed_domains


async def resolve_web_people(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    identity: VerifiedIdentity,
    settings: WebSettings,
    new_id: Callable[[], uuid.UUID] = uuid.uuid4,
) -> list[WebPersonRow]:
    """The people this sign-in acts as, creating them on first sign-in."""
    if identity.provider != "oidc":
        return await _platform_people(session_factory, identity=identity)
    oidc = settings.oidc
    if oidc is None or oidc.issuer != identity.issuer:
        raise WebSignInRefused("This way of signing in isn't available.")
    if not email_domain_allowed(identity, oidc.allowed_domains):
        raise WebSignInRefused("This account can't sign in here. Ask an admin.")
    async with session_factory() as session, session.begin():
        return [await _oidc_person(session, identity=identity, oidc=oidc, new_id=new_id)]


async def _oidc_person(
    session: AsyncSession,
    *,
    identity: VerifiedIdentity,
    oidc: WebOidcSettings,
    new_id: Callable[[], uuid.UUID],
) -> WebPersonRow:
    tenant = await tenants_store.get_tenant(session, oidc.tenant_id)
    if tenant is None or tenant.archived_at is not None or tenant.provision_status != "ready":
        raise WebSignInRefused("This workspace isn't ready yet. Ask an admin.")
    if not await _signin_on(session, tenant.id, "oidc"):
        raise WebSignInRefused(_TURNED_OFF)
    known = [
        row
        for row in await identities_store.find_identity(
            session, provider="oidc", issuer=identity.issuer, subject=identity.subject
        )
        if row.tenant_id == tenant.id
    ]
    person_id = known[0].person_id if known else new_id()
    existing = (
        await people_store.get_person(session, tenant_id=tenant.id, person_id=person_id)
        if known
        else None
    )
    if existing is not None:
        account_id = existing.account_id
    else:
        # The principal's external id is the person id, so it exists before the person row.
        principal = await get_or_create_platform_principal(
            session, tenant_id=tenant.id, platform="web", external_id=str(person_id)
        )
        account_id = principal.account_id
    person = await people_store.upsert_member(
        session,
        tenant_id=tenant.id,
        account_id=account_id,
        display_name=identity.display_name,
        workspace_name=oidc.workspace_name,
        email=identity.email,
        new_person_id=person_id,
    )
    await identities_store.link_identity(
        session,
        tenant_id=tenant.id,
        person_id=person.id,
        provider="oidc",
        issuer=identity.issuer,
        subject=identity.subject,
        label=identity.email or identity.display_name,
        groups=identity.groups,
    )
    return person


async def _platform_people(
    session_factory: async_sessionmaker[AsyncSession], *, identity: VerifiedIdentity
) -> list[WebPersonRow]:
    """A member row in every workspace the chat account is in and Daimon is installed in.

    The person runs as their platform principal there, the one the chat adapters use,
    so spend, memory and roles are the same on the web as in chat.
    """
    if identity.platform_user_id is None:
        raise WebSignInRefused("This way of signing in isn't available.")
    tenants = await resolve_hub_tenants(
        session_factory,
        platform=PLATFORM_OF[identity.provider],
        platform_user_id=identity.platform_user_id,
        workspaces=identity.workspaces,
    )
    if not tenants:
        raise WebSignInRefused("Daimon isn't in any of your workspaces yet. Ask an admin.")
    people: list[WebPersonRow] = []
    async with session_factory() as session, session.begin():
        for tenant in tenants:
            if not await _signin_on(session, tenant.tenant_id, identity.provider):
                continue
            person = await people_store.upsert_member(
                session,
                tenant_id=tenant.tenant_id,
                account_id=tenant.account_id,
                display_name=identity.display_name,
                workspace_name=tenant.workspace_name,
                email=identity.email,
                new_person_id=uuid.uuid4(),
            )
            await identities_store.link_identity(
                session,
                tenant_id=tenant.tenant_id,
                person_id=person.id,
                provider=identity.provider,
                issuer="",
                subject=identity.subject,
                label=identity.display_name,
                groups=(),
            )
            people.append(person)
    if not people:
        raise WebSignInRefused(_TURNED_OFF)
    return people


async def _signin_on(session: AsyncSession, tenant_id: uuid.UUID, provider: str) -> bool:
    """Whether the workspace's admins left this way of signing in on."""
    chosen = await workspace_settings.get_settings(session, tenant_id=tenant_id)
    return provider not in chosen.signin_off


async def _has_history(session: AsyncSession, person: WebPersonRow, user_id: str) -> bool:
    """Whether anything is recorded under the person's own `web` id: chats, spend or limits."""
    if await chat_store.list_threads(session, person_id=person.id):
        return True
    if await usage_events.turn_count_for_user_in_tenant_since(
        session, tenant_id=person.tenant_id, platform_user_id=user_id, since=_EPOCH
    ):
        return True
    allowances = await spend_allowances.list_for_tenant(session, tenant_id=person.tenant_id)
    return any(a.subject_kind == "person" and a.subject_id == user_id for a in allowances)


async def link_web_identity(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    person: WebPersonRow,
    identity: VerifiedIdentity,
    now: datetime,
) -> WebPersonRow:
    """Link a chat account to a signed-in person, proven by signing in with it.

    Only the platform id links, never an email. A chat id Daimon has not seen yet joins
    the person's account. One it has seen has its own account; the person moves onto it
    only if nothing is recorded under their own `web` id, since merging two accounts'
    spend and memory is an admin's call.
    """
    user_id = identity.platform_user_id
    if identity.provider == "oidc" or user_id is None:
        raise WebSignInRefused("This way of signing in can't be linked.")
    platform = PLATFORM_OF[identity.provider]
    async with session_factory() as session, session.begin():
        tenant = await tenants_store.get_tenant(session, person.tenant_id)
        workspace_ids = {workspace_id for workspace_id, _ in identity.workspaces}
        if tenant is None or tenant.platform != platform or tenant.external_id not in workspace_ids:
            raise WebSignInRefused("This account isn't in this workspace.")
        known = await identities_store.find_identity(
            session, provider=identity.provider, issuer="", subject=identity.subject
        )
        if any(row.tenant_id == tenant.id and row.person_id != person.id for row in known):
            raise WebSignInRefused("This account is linked to someone else. Ask an admin.")
        principal = await find_platform_principal(
            session, tenant_id=tenant.id, platform=platform, external_id=user_id
        )
        if principal is None:
            await create_platform_principal(
                session,
                tenant_id=tenant.id,
                platform=platform,
                external_id=user_id,
                account_id=person.account_id,
            )
        elif principal.account_id != person.account_id:
            if await _has_history(session, person, str(person.id)):
                raise WebSignInRefused("This account already has its own history. Ask an admin.")
            taken = await people_store.get_person_for_account(
                session, tenant_id=tenant.id, account_id=principal.account_id
            )
            if taken is not None:
                raise WebSignInRefused("This account is linked to someone else. Ask an admin.")
            carried = await _carry_role(session, tenant.id, person.account_id, principal.account_id)
            await people_store.update_person(
                session, person_id=person.id, account_id=principal.account_id
            )
            if carried is not None:
                await web_audit.record(
                    session,
                    tenant_id=tenant.id,
                    actor_account_id=principal.account_id,
                    action="Moved role to linked account",
                    target=web_audit.PERSON_LABEL,
                    target_person_id=person.id,
                    detail=carried.capitalize(),
                    at=now,
                )
            await web_audit.record(
                session,
                tenant_id=tenant.id,
                actor_account_id=principal.account_id,
                action="Switched account on link",
                target=web_audit.PERSON_LABEL,
                target_person_id=person.id,
                detail=identity.provider.capitalize(),
                at=now,
            )
        await identities_store.link_identity(
            session,
            tenant_id=tenant.id,
            person_id=person.id,
            provider=identity.provider,
            issuer="",
            subject=identity.subject,
            label=identity.display_name,
            groups=(),
        )
        linked = await people_store.get_person(session, tenant_id=tenant.id, person_id=person.id)
    assert linked is not None
    return linked


async def _carry_role(
    session: AsyncSession, tenant_id: uuid.UUID, old: uuid.UUID, new: uuid.UUID
) -> str | None:
    """Move a web role off the account the person leaves, unless the new one ranks higher.

    The role the new account holds afterwards, or None when the old one held none."""
    grant = await roles_store.get_workspace_role(session, tenant_id=tenant_id, account_id=old)
    if grant is None:
        return None
    current = await roles_store.get_workspace_role(session, tenant_id=tenant_id, account_id=new)
    role = "owner" if "owner" in (grant, current) else "admin"
    if current != "owner":
        await roles_store.set_workspace_role(
            session,
            tenant_id=tenant_id,
            account_id=new,
            role=role,
            granted_by_account_id=None,
        )
    await roles_store.delete_workspace_role(session, tenant_id=tenant_id, account_id=old)
    return role


WebRoleName = Literal["owner", "admin", "member", "visitor"]
"""A person's role in their workspace, highest first."""

#: Platforms whose principal a linked web person runs as, so spend and memory stay theirs.
_LINKABLE: Final = ("discord", "slack", "teams")


def thread_key(thread_id: uuid.UUID) -> str:
    """The thread id a web chat is recorded under, in turns, spend and bindings."""
    return f"{WEB_PLACE_PREFIX}{thread_id}"


def place_of(person_id: uuid.UUID) -> str:
    """The place a person's chats are in. One per person, so what a place records (a
    shared channel, a hold, memory) never crosses from one person to another. Surface
    checks use `Surface.WEB`, never this id."""
    return f"{WEB_PLACE_PREFIX}{person_id}"


@dataclass(frozen=True)
class WebPrincipal:
    """Who a person's turns run as, and how admission sees them."""

    platform: str
    external_id: str
    role: Role
    org: RequesterOrg
    home_id: str | None
    groups: frozenset[str] = frozenset()
    # Who is spending, for allowances: every id, group and invite of the person's account.
    # None until `with_spend_person`, so listings never pay for it; the gate then resolves it.
    spend_person: SpendPerson | None = None

    @property
    def subject(self) -> Subject:
        return Subject(
            is_admin=self.role is Role.ADMIN,
            platform_user_id=self.external_id,
            client=self.org is RequesterOrg.OTHER,
        )


async def linked_identity(session: AsyncSession, person: WebPersonRow) -> tuple[str, str]:
    """`(platform, user id)` a person's turns and spend are recorded under.

    A member who linked a chat account runs as it; a visitor never does, and anyone else
    runs as their own `web` id. The first link by platform order and age, so a second
    link never moves spend between ids.
    """
    if person.kind == "visitor":
        return "web", str(person.id)
    linked = [
        p
        for p in await list_platform_principals_for_account(session, account_id=person.account_id)
        if p.platform in _LINKABLE and p.tenant_id == person.tenant_id
    ]
    if not linked:
        return "web", str(person.id)
    first = min(linked, key=lambda p: (_LINKABLE.index(p.platform), p.created_at))
    return first.platform, first.external_id


async def principal_of(
    session: AsyncSession,
    person: WebPersonRow,
    role: WebRoleName,
    *,
    groups: frozenset[str] = frozenset(),
) -> WebPrincipal:
    """A linked chat account if the person has one, else their own `web` principal."""
    platform, external_id = await linked_identity(session, person)
    visitor = person.kind == "visitor"
    return WebPrincipal(
        platform=platform,
        external_id=external_id,
        role=Role.ADMIN if role in ("owner", "admin") else Role.USER,
        org=RequesterOrg.OTHER if visitor else RequesterOrg.HOST,
        home_id=f"invite:{person.invite_id}" if visitor else None,
        groups=groups,
    )


async def with_spend_person(
    session: AsyncSession, person: WebPersonRow, principal: WebPrincipal
) -> WebPrincipal:
    """`principal` with who is spending resolved, for the paths that run a spend gate."""
    spend_person = await spend_person_of_account(
        session,
        tenant_id=person.tenant_id,
        account_id=person.account_id,
        platform_user_id=principal.external_id,
    )
    return replace(principal, spend_person=spend_person)
