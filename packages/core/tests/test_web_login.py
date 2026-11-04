"""Connected accounts: a login remembers the chat accounts one member signed in with."""

from __future__ import annotations

import secrets
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from daimon.core.stores import web_logins as logins_store
from daimon.core.stores import web_people as people_store
from daimon.core.stores import web_sessions as sessions_store
from daimon.core.stores.domain import WebPersonRow, WebSessionRow
from daimon.core.web_identity import VerifiedIdentity, WebSignInRefused, resolve_web_people
from daimon.core.web_login import (
    LOGIN_MEMORY,
    ConnectedAccountNotFound,
    connect_account,
    disconnect_account,
    sign_in_login,
)
from daimon.testing.factories import make_tenant
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

NOW = datetime(2030, 1, 1, 12, tzinfo=UTC)
TOKEN = b"t" * 32
Factory = async_sessionmaker[AsyncSession]


def _discord(*workspaces: tuple[str, str], subject: str = "d1") -> VerifiedIdentity:
    return VerifiedIdentity(
        provider="discord", subject=subject, display_name="Ada", workspaces=workspaces
    )


def _slack(*workspaces: tuple[str, str], user: str = "U1") -> VerifiedIdentity:
    return VerifiedIdentity(
        provider="slack",
        subject=f"T1:{user}",
        display_name="Ada",
        workspaces=workspaces,
        platform_user_id=user,
    )


async def _sign_in(
    factory: Factory,
    identity: VerifiedIdentity,
    *,
    now: datetime = NOW,
    token: bytes | None = None,
) -> tuple[WebSessionRow, list[WebPersonRow]]:
    """What the callback does: resolve the account's people, join its login, start a session."""
    people = await resolve_web_people(factory, identity=identity)
    login_id, reached = await sign_in_login(factory, identity=identity, people=people, now=now)
    async with factory.begin() as db:
        row = await sessions_store.create_session(
            db,
            token_sha256=token or secrets.token_bytes(32),
            provider=identity.provider,
            issuer="",
            subject=identity.subject,
            people=reached,
            ttl=timedelta(days=1),
            now=now,
            login_id=login_id,
        )
    return row, reached


async def _tenants(factory: Factory, session_id: uuid.UUID) -> set[uuid.UUID]:
    async with factory() as db:
        ids = await sessions_store.list_session_people(db, session_id=session_id)
        return {p.tenant_id for p in await people_store.get_people(db, person_ids=ids)}


async def _two_workspaces(db_session: AsyncSession) -> tuple[uuid.UUID, uuid.UUID]:
    discord = await make_tenant(db_session, platform="discord", workspace_id="g1")
    slack = await make_tenant(db_session, platform="slack", workspace_id="T1")
    await db_session.commit()
    return discord.id, slack.id


async def _connected(
    factory: Factory, db_session: AsyncSession
) -> tuple[WebSessionRow, list[WebPersonRow], uuid.UUID, uuid.UUID]:
    """A Discord sign-in in g1 that has connected Slack T1."""
    g1, t1 = await _two_workspaces(db_session)
    web_session, people = await _sign_in(factory, _discord(("g1", "Acme")), token=TOKEN)
    await connect_account(
        factory,
        web_session=web_session,
        person=people[0],
        identity=_slack(("T1", "Beta")),
        now=NOW,
    )
    return web_session, people, g1, t1


async def test_connect_adds_the_accounts_workspaces_to_the_session(
    db_session: AsyncSession, db_session_factory: Factory
) -> None:
    web_session, _, g1, t1 = await _connected(db_session_factory, db_session)

    assert await _tenants(db_session_factory, web_session.id) == {g1, t1}, (
        "the session reaches the Discord workspace and the connected Slack one"
    )
    async with db_session_factory() as db:
        accounts = await logins_store.list_accounts(
            db, login_id=web_session.login_id or uuid.uuid4()
        )
    assert {a.provider for a in accounts} == {"discord", "slack"}, "both accounts share a login"


async def test_either_connected_account_signs_in_to_every_workspace(
    db_session: AsyncSession, db_session_factory: Factory
) -> None:
    _, _, g1, t1 = await _connected(db_session_factory, db_session)

    for identity in (_slack(("T1", "Beta")), _discord(("g1", "Acme"))):
        later, _ = await _sign_in(db_session_factory, identity, now=NOW + timedelta(days=1))
        assert await _tenants(db_session_factory, later.id) == {g1, t1}, (
            f"signing in with {identity.provider} shows both accounts' workspaces"
        )


async def test_an_account_not_proven_for_thirty_days_is_skipped(
    db_session: AsyncSession, db_session_factory: Factory
) -> None:
    _, _, g1, t1 = await _connected(db_session_factory, db_session)
    lapsed = NOW + LOGIN_MEMORY + timedelta(hours=1)

    later, _ = await _sign_in(db_session_factory, _discord(("g1", "Acme")), now=lapsed)
    assert await _tenants(db_session_factory, later.id) == {g1}, "Slack lapsed; only Discord shows"

    again, _ = await _sign_in(db_session_factory, _slack(("T1", "Beta")), now=lapsed)
    assert await _tenants(db_session_factory, again.id) == {g1, t1}, (
        "proving Slack again brings it back, and Discord was proven just before"
    )


async def test_connecting_an_account_of_another_login_merges_the_logins(
    db_session: AsyncSession, db_session_factory: Factory
) -> None:
    g1, t1 = await _two_workspaces(db_session)
    other, _ = await _sign_in(db_session_factory, _slack(("T1", "Beta")))
    async with db_session_factory.begin() as db:
        await logins_store.add_account(
            db,
            login_id=other.login_id or uuid.uuid4(),
            provider="microsoft",
            subject="m1",
            display_name="Ada",
            now=NOW,
        )
    web_session, people = await _sign_in(db_session_factory, _discord(("g1", "Acme")))

    result = await connect_account(
        db_session_factory,
        web_session=web_session,
        person=people[0],
        identity=_slack(("T1", "Beta")),
        now=NOW,
    )

    async with db_session_factory() as db:
        accounts = await logins_store.list_accounts(
            db, login_id=web_session.login_id or uuid.uuid4()
        )
        left_over = await logins_store.list_accounts(db, login_id=other.login_id or uuid.uuid4())
    assert result.merged, "the account was in another login"
    assert {a.provider for a in accounts} == {"discord", "slack", "microsoft"}, (
        "the other login's accounts came along"
    )
    assert left_over == [], "the other login is gone"
    assert await _tenants(db_session_factory, web_session.id) == {g1, t1}


async def test_a_visitor_cannot_connect(
    db_session: AsyncSession, db_session_factory: Factory
) -> None:
    await _two_workspaces(db_session)
    web_session, people = await _sign_in(db_session_factory, _discord(("g1", "Acme")))
    visitor = people[0].model_copy(update={"kind": "visitor"})

    with pytest.raises(WebSignInRefused):
        await connect_account(
            db_session_factory,
            web_session=web_session,
            person=visitor,
            identity=_slack(("T1", "Beta")),
            now=NOW,
        )
    async with db_session_factory() as db:
        accounts = await logins_store.list_accounts(
            db, login_id=web_session.login_id or uuid.uuid4()
        )
    assert [a.provider for a in accounts] == ["discord"], "nothing was connected"


async def test_connect_is_refused_when_daimon_is_in_none_of_the_accounts_workspaces(
    db_session: AsyncSession, db_session_factory: Factory
) -> None:
    await _two_workspaces(db_session)
    web_session, people = await _sign_in(db_session_factory, _discord(("g1", "Acme")))

    with pytest.raises(WebSignInRefused):
        await connect_account(
            db_session_factory,
            web_session=web_session,
            person=people[0],
            identity=_slack(("T9", "Elsewhere")),
            now=NOW,
        )
    async with db_session_factory() as db:
        account = await logins_store.get_account(db, provider="slack", subject="T1:U1")
    assert account is None, "the refused account was not added to the login"


async def test_a_session_never_holds_two_people_of_one_workspace(
    db_session: AsyncSession, db_session_factory: Factory
) -> None:
    g1, _ = await _two_workspaces(db_session)
    web_session, people = await _sign_in(db_session_factory, _discord(("g1", "Acme")))

    result = await connect_account(
        db_session_factory,
        web_session=web_session,
        person=people[0],
        identity=_discord(("g1", "Acme"), subject="d2"),
        now=NOW,
    )

    assert result.added == [], "the second Discord account's person in g1 is not added"
    assert await _tenants(db_session_factory, web_session.id) == {g1}
    async with db_session_factory() as db:
        ids = await sessions_store.list_session_people(db, session_id=web_session.id)
    assert ids == [people[0].id], "the person already there stays"


async def test_disconnect_removes_the_account_and_its_workspaces(
    db_session: AsyncSession, db_session_factory: Factory
) -> None:
    web_session, people, g1, _ = await _connected(db_session_factory, db_session)

    await disconnect_account(
        db_session_factory,
        web_session=web_session,
        person=people[0],
        provider="slack",
        subject="T1:U1",
        now=NOW,
    )

    assert await _tenants(db_session_factory, web_session.id) == {g1}, "the Slack workspace left"
    async with db_session_factory() as db:
        gone = await logins_store.get_account(db, provider="slack", subject="T1:U1")
    assert gone is None, "the account left the login"


async def test_disconnect_moves_off_the_workspace_it_removes(
    db_session: AsyncSession, db_session_factory: Factory
) -> None:
    web_session, _, g1, t1 = await _connected(db_session_factory, db_session)
    async with db_session_factory.begin() as db:
        switched = await sessions_store.switch_person(db, session_id=web_session.id, tenant_id=t1)
    assert switched is not None
    async with db_session_factory() as db:
        (current,) = await people_store.get_people(db, person_ids=[switched.current_person_id])

    await disconnect_account(
        db_session_factory,
        web_session=web_session,
        person=current,
        provider="slack",
        subject="T1:U1",
        now=NOW,
    )

    async with db_session_factory() as db:
        live = await sessions_store.get_live_session(db, token_sha256=TOKEN, now=NOW)
    assert live is not None and live.tenant_id == g1, "the session is back on the Discord workspace"


async def test_the_account_a_session_signed_in_with_cannot_be_disconnected(
    db_session: AsyncSession, db_session_factory: Factory
) -> None:
    web_session, people, _, _ = await _connected(db_session_factory, db_session)

    with pytest.raises(WebSignInRefused):
        await disconnect_account(
            db_session_factory,
            web_session=web_session,
            person=people[0],
            provider="discord",
            subject="d1",
            now=NOW,
        )
    with pytest.raises(ConnectedAccountNotFound):
        await disconnect_account(
            db_session_factory,
            web_session=web_session,
            person=people[0],
            provider="slack",
            subject="T1:unknown",
            now=NOW,
        )
