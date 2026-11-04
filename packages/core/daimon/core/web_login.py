"""Connected accounts: one login groups the chat accounts a member signs in with.

Signing in with any account shows the workspaces of the others, while each was proven
within `LOGIN_MEMORY`. Connecting proves another account from a signed-in session and adds
its workspaces; disconnecting takes them away again, ends every session signed in with it
and takes its people out of the login's other sessions. A remembered account's people join
only where the workspace's synced member list still names them (Teams has none: the window
alone applies). A session never holds two people of one workspace: the one already there stays.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final

from daimon.core.errors import UserFacingError
from daimon.core.stores import tenants as tenants_store
from daimon.core.stores import web_audit
from daimon.core.stores import web_identities as identities_store
from daimon.core.stores import web_logins as logins_store
from daimon.core.stores import web_people as people_store
from daimon.core.stores import web_sessions as sessions_store
from daimon.core.stores import workspace_members as members_store
from daimon.core.stores.domain import WebLoginSessionPersonRow, WebPersonRow, WebSessionRow
from daimon.core.web_identity import (
    PLATFORM_OF,
    VerifiedIdentity,
    WebSignInRefused,
    link_web_identity,
    resolve_web_people,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

#: How long a connected account's workspaces keep joining a sign-in after it last proved itself.
LOGIN_MEMORY: Final = timedelta(days=30)

_INVITE = "invite"
#: Providers whose workspaces have no member list to check a remembered person against.
#: Teams has none, so there the window alone applies; every other platform needs a synced
#: list that still names the person, or a fresh sign-in with that account.
_UNLISTED: Final = frozenset({"microsoft"})
_SIGN_IN_AGAIN = "Sign in again."


class ConnectedAccountNotFound(UserFacingError):
    """The account is not one of this sign-in's connected accounts."""


@dataclass(frozen=True, slots=True)
class ConnectResult:
    added: list[WebPersonRow]
    merged: bool


def one_per_tenant(people: list[WebPersonRow]) -> list[WebPersonRow]:
    """The first person of each workspace, in order."""
    seen: set[uuid.UUID] = set()
    out: list[WebPersonRow] = []
    for person in people:
        if person.tenant_id not in seen:
            seen.add(person.tenant_id)
            out.append(person)
    return out


async def _remembered(
    session: AsyncSession,
    *,
    login_id: uuid.UUID,
    skip: tuple[str, str],
    now: datetime,
) -> list[WebPersonRow]:
    """Active members of the login's other accounts that proved themselves recently and
    can be checked: a workspace whose synced member list still names them (Teams has no
    list, so the window alone stands)."""
    accounts = await logins_store.list_accounts(session, login_id=login_id)
    recent = {
        (a.provider, a.subject)
        for a in accounts
        if (a.provider, a.subject) != skip and a.last_proven_at >= now - LOGIN_MEMORY
    }
    reached = await identities_store.list_active_members_of_accounts(session, accounts=recent)
    standings = await members_store.list_chat_standings(
        session, people={(p.tenant_id, p.account_id) for p, _ in reached}
    )
    kept: list[WebPersonRow] = []
    for person, provider in reached:
        standing = standings.get((person.tenant_id, person.account_id))
        if provider in _UNLISTED or (standing is not None and standing.synced and standing.member):
            kept.append(person)
    return kept


async def _require_own_account(session: AsyncSession, web_session: WebSessionRow) -> None:
    """The account the session signed in with must still be in its login: disconnecting it
    from another device ends this session's right to manage the rest. Invite sessions
    signed in with no account."""
    if web_session.provider == _INVITE:
        return
    own = await logins_store.get_account(
        session, provider=web_session.provider, subject=web_session.subject
    )
    if own is None or own.login_id != web_session.login_id:
        raise WebSignInRefused(_SIGN_IN_AGAIN)


async def sign_in_login(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    identity: VerifiedIdentity,
    people: list[WebPersonRow],
    now: datetime,
) -> tuple[uuid.UUID, list[WebPersonRow]]:
    """Note the account's sign-in on its login. Returns the login and the people the
    session reaches: the account's own, then the connected accounts' recent ones."""
    async with session_factory() as session, session.begin():
        account = await logins_store.prove_account(
            session,
            provider=identity.provider,
            subject=identity.subject,
            display_name=identity.display_name,
            now=now,
        )
        remembered = await _remembered(
            session,
            login_id=account.login_id,
            skip=(identity.provider, identity.subject),
            now=now,
        )
    return account.login_id, one_per_tenant([*people, *remembered])


async def _link_first(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    person: WebPersonRow,
    identity: VerifiedIdentity,
    now: datetime,
) -> None:
    """Attach the account to the current person when it is the same platform and
    workspace and they have no account of that provider yet (a member who joined by
    invite), using the existing link and all its checks."""
    async with session_factory() as session:
        tenant = await tenants_store.get_tenant(session, person.tenant_id)
        held = await identities_store.list_for_person(session, person_id=person.id)
    if (
        tenant is not None
        and tenant.platform == PLATFORM_OF[identity.provider]
        and tenant.external_id in {workspace for workspace, _ in identity.workspaces}
        and not any(row.provider == identity.provider for row in held)
    ):
        await link_web_identity(session_factory, person=person, identity=identity, now=now)


async def connect_account(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    web_session: WebSessionRow,
    person: WebPersonRow,
    identity: VerifiedIdentity,
    now: datetime,
) -> ConnectResult:
    """Add the account the provider just proved to the session's login and its workspaces
    to the session. Refused as signing in with it would be."""
    if person.kind == "visitor":
        raise WebSignInRefused("Guests can't connect an account.")
    async with session_factory() as session:
        await _require_own_account(session, web_session)
    await _link_first(session_factory, person=person, identity=identity, now=now)
    people = await resolve_web_people(session_factory, identity=identity)
    async with session_factory() as session, session.begin():
        login_id = web_session.login_id
        if login_id is None:
            login_id = await logins_store.create_login(session)
            await logins_store.attach_session(session, session_id=web_session.id, login_id=login_id)
        merged_from = await logins_store.add_account(
            session,
            login_id=login_id,
            provider=identity.provider,
            subject=identity.subject,
            display_name=identity.display_name,
            now=now,
        )
        joining = people
        if merged_from is not None:
            joining = [
                *people,
                *await _remembered(
                    session,
                    login_id=login_id,
                    skip=(identity.provider, identity.subject),
                    now=now,
                ),
            ]
        added = await sessions_store.add_people(
            session, session_id=web_session.id, people=one_per_tenant(joining)
        )
        await _audit(session, person, "Connected account", identity.provider, now)
        if merged_from is not None:
            await _audit(session, person, "Merged logins", identity.provider, now)
    return ConnectResult(added=added, merged=merged_from is not None)


async def disconnect_account(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    web_session: WebSessionRow,
    person: WebPersonRow,
    provider: str,
    subject: str,
    now: datetime,
) -> None:
    """Take a connected account out of the session's login, and its workspaces out of the
    session, moving off the current one if it was only reached through that account.

    Refused for the account the session signed in with.
    """
    if (provider, subject) == (web_session.provider, web_session.subject):
        raise WebSignInRefused("You signed in with this account.")
    async with session_factory() as session, session.begin():
        await _require_own_account(session, web_session)
        account = await logins_store.get_account(session, provider=provider, subject=subject)
        login_id = web_session.login_id
        if account is None or login_id is None or account.login_id != login_id:
            raise ConnectedAccountNotFound("This account isn't connected.")
        await logins_store.remove_account(session, provider=provider, subject=subject)
        await sessions_store.delete_signed_in_with(session, provider=provider, subject=subject)
        keep_via = {
            (a.provider, a.subject)
            for a in await logins_store.list_accounts(session, login_id=login_id)
        }
        reached = await sessions_store.list_login_people(session, login_id=login_id)
        held = await identities_store.list_for_people(
            session, person_ids={r.person_id for r in reached}
        )
        gone = {
            person_id
            for person_id, rows in held.items()
            if any((r.provider, r.subject) == (provider, subject) for r in rows)
            and not any(r.provider == _INVITE or (r.provider, r.subject) in keep_via for r in rows)
        }
        by_session: dict[uuid.UUID, list[WebLoginSessionPersonRow]] = {}
        for row in reached:
            by_session.setdefault(row.session_id, []).append(row)
        for session_id, rows in by_session.items():
            dropped = [r.person_id for r in rows if r.person_id in gone]
            if not dropped:
                continue
            await sessions_store.remove_people(session, session_id=session_id, person_ids=dropped)
            if rows[0].current_person_id not in dropped:
                continue
            left = await people_store.get_people(
                session, person_ids=[r.person_id for r in rows if r.person_id not in gone]
            )
            target = next((p for p in left if p.status == "active"), left[0] if left else None)
            if target is not None:
                await sessions_store.switch_person(
                    session, session_id=session_id, tenant_id=target.tenant_id
                )
            elif session_id == web_session.id:
                raise WebSignInRefused("This is the only way into your workspaces.")
            else:
                await sessions_store.delete_by_ids(session, session_ids=[session_id])
        await _audit(session, person, "Disconnected account", provider, now)


async def _audit(
    session: AsyncSession, person: WebPersonRow, action: str, provider: str, now: datetime
) -> None:
    await web_audit.record(
        session,
        tenant_id=person.tenant_id,
        actor_account_id=person.account_id,
        action=action,
        target=web_audit.PERSON_LABEL,
        target_person_id=person.id,
        detail=provider.capitalize(),
        at=now,
    )
