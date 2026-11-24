"""Google sign-in for guests: a confirmed email finds a guest, a pin keeps it, members never match."""

from __future__ import annotations

import uuid
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
from daimon.core.stores import web_identities as identities_store
from daimon.core.stores import web_people as people_store
from daimon.core.stores.domain import WebPersonRow
from daimon.testing.outsiders import hold_audience, link_discord_member
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .chat_support import funded_workspace, visitor_of
from .conftest import (
    PUBLIC_URL,
    FakeUpstream,
    WebClientFactory,
    fake_google,
    web_settings,
)

_OWN = {"X-Daimon-Web": "1", "Origin": PUBLIC_URL}
_NO_ACCESS = "Daimon isn't in any of your workspaces yet. Ask an admin."


def _google_web(**overrides: object) -> Any:
    return web_settings(google_client_id="gid", google_client_secret="gsecret", **overrides)


async def _sign_in(
    web_client: WebClientFactory,
    factory: async_sessionmaker[AsyncSession],
    upstream: FakeUpstream,
) -> tuple[httpx.Response, dict[str, Any] | None]:
    """Walk the Google redirect flow; return the callback and `/api/me` if it signed in."""
    async with web_client(_google_web(), sessionmaker=factory, provider=upstream) as client:
        start = await client.get("/auth/google/start")
        state = parse_qs(urlsplit(start.headers["location"]).query)["state"][0]
        back = await client.get("/auth/google/callback", params={"code": "c", "state": state})
        error = parse_qs(urlsplit(back.headers["location"]).query).get("error")
        me = None if error else (await client.get("/api/me")).json()
    return back, me


def _refusal(back: httpx.Response) -> str | None:
    query = parse_qs(urlsplit(back.headers["location"]).query)
    return query["error"][0] if "error" in query else None


async def _pins(factory: async_sessionmaker[AsyncSession], sub: str) -> list[uuid.UUID]:
    async with factory() as db:
        rows = await identities_store.find_identity(db, provider="google", issuer="", subject=sub)
    return [r.person_id for r in rows]


async def test_google_is_offered_only_when_configured_and_never_connectable(
    web_client: WebClientFactory,
) -> None:
    async with web_client() as client:
        bare = (await client.get("/auth/providers")).json()
    async with web_client(_google_web()) as client:
        listed = (await client.get("/auth/providers")).json()
        connect = await client.post("/auth/google/connect", json={}, headers=_OWN)
    assert bare == [], "no Google client, no Google button"
    assert listed == [{"id": "google", "label": "Google", "kind": "redirect", "connectable": False}]
    assert connect.status_code == 404, "a member cannot connect Google as a chat account"


async def test_a_confirmed_email_signs_in_the_guest_it_names_and_pins_the_account(
    web_client: WebClientFactory, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    ws = await funded_workspace(db_session_factory)
    guest = await visitor_of(db_session_factory, ws, email="Gus@Example.com")

    back, me = await _sign_in(
        web_client, db_session_factory, fake_google(sub="g-1", email="gus@example.com")
    )

    assert _refusal(back) is None, back.headers
    assert me is not None and me["role"] == "visitor", me
    assert await _pins(db_session_factory, "g-1") == [guest.id], (
        "the account is pinned to the guest"
    )


async def test_a_pinned_account_signs_in_without_its_email(
    web_client: WebClientFactory, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    ws = await funded_workspace(db_session_factory)
    guest = await visitor_of(db_session_factory, ws, email="gus@example.com")
    await _sign_in(web_client, db_session_factory, fake_google(sub="g-1", email="gus@example.com"))

    back, me = await _sign_in(
        web_client, db_session_factory, fake_google(sub="g-1", email="other@example.com")
    )

    assert _refusal(back) is None and me is not None, "the pin, not the email, finds the guest"
    assert await _pins(db_session_factory, "g-1") == [guest.id], "the pin is reused, not repeated"


async def test_an_unconfirmed_email_is_refused_and_pins_nothing(
    web_client: WebClientFactory, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    ws = await funded_workspace(db_session_factory)
    await visitor_of(db_session_factory, ws, email="gus@example.com")

    back, _ = await _sign_in(
        web_client,
        db_session_factory,
        fake_google(sub="g-1", email="gus@example.com", verified=False),
    )

    assert _refusal(back) == "Google hasn't confirmed your email address.", back.headers
    assert await _pins(db_session_factory, "g-1") == [], "nothing was pinned"


async def test_a_members_email_never_signs_in_with_google(
    web_client: WebClientFactory, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    ws = await funded_workspace(db_session_factory)  # its member is ada@example.com

    back, _ = await _sign_in(
        web_client, db_session_factory, fake_google(sub="g-2", email=ws.person.email)
    )

    assert _refusal(back) == _NO_ACCESS, "members sign in with their chat account"
    assert await _pins(db_session_factory, "g-2") == [], "a member is never pinned"


async def test_an_outsiders_email_never_signs_in_with_google(
    web_client: WebClientFactory, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """An outsider counts as a visitor for pages, but is a member row: no email match."""
    ws = await funded_workspace(db_session_factory)
    async with db_session_factory.begin() as session:
        await hold_audience(session, ws.tenant_id)
        await link_discord_member(
            session,
            tenant_id=ws.tenant_id,
            account_id=ws.person.account_id,
            internal=False,
            discord_id="7001",
        )

    back, _ = await _sign_in(
        web_client, db_session_factory, fake_google(sub="g-3", email=ws.person.email)
    )

    assert _refusal(back) == _NO_ACCESS, "an outsider signs in with Discord, not by email"
    assert await _pins(db_session_factory, "g-3") == [], "an outsider is never pinned"


async def test_an_account_matching_no_guest_is_refused(
    web_client: WebClientFactory, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    ws = await funded_workspace(db_session_factory)
    await visitor_of(db_session_factory, ws, email="gus@example.com")

    back, _ = await _sign_in(
        web_client, db_session_factory, fake_google(sub="g-3", email="stranger@example.com")
    )

    assert _refusal(back) == _NO_ACCESS, back.headers


async def test_a_guest_with_another_google_account_is_not_taken_by_email(
    web_client: WebClientFactory, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    ws = await funded_workspace(db_session_factory)
    guest = await visitor_of(db_session_factory, ws, email="gus@example.com")
    await _sign_in(web_client, db_session_factory, fake_google(sub="g-1", email="gus@example.com"))

    back, _ = await _sign_in(
        web_client, db_session_factory, fake_google(sub="g-4", email="gus@example.com")
    )

    assert _refusal(back) == _NO_ACCESS, "the guest already belongs to another Google account"
    assert await _pins(db_session_factory, "g-1") == [guest.id], "and keeps it"


async def test_one_email_guest_in_several_workspaces_reaches_each_and_suspended_ones_none(
    web_client: WebClientFactory, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    first = await funded_workspace(db_session_factory)
    second = await funded_workspace(db_session_factory)
    third = await funded_workspace(db_session_factory)
    guests = [
        await visitor_of(db_session_factory, ws, email="gus@example.com")
        for ws in (first, second, third)
    ]
    async with db_session_factory.begin() as db:
        await people_store.update_person(db, person_id=guests[2].id, status="suspended")

    back, me = await _sign_in(
        web_client, db_session_factory, fake_google(sub="g-5", email="gus@example.com")
    )

    assert _refusal(back) is None and me is not None, back.headers
    assert urlsplit(back.headers["location"]).path == "/workspaces", "more than one to pick from"
    assert sorted(await _pins(db_session_factory, "g-5")) == sorted(g.id for g in guests[:2]), (
        "both active guests are pinned, the suspended one is not"
    )
    assert len(me["workspaces"]) == 2, me["workspaces"]


async def test_a_later_invitation_reaches_an_account_pinned_elsewhere(
    web_client: WebClientFactory, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    first = await funded_workspace(db_session_factory)
    await visitor_of(db_session_factory, first, email="gus@example.com")
    await _sign_in(web_client, db_session_factory, fake_google(sub="g-6", email="gus@example.com"))
    second = await funded_workspace(db_session_factory)
    invited: WebPersonRow = await visitor_of(db_session_factory, second, email="gus@example.com")

    _, me = await _sign_in(
        web_client, db_session_factory, fake_google(sub="g-6", email="gus@example.com")
    )

    assert invited.id in await _pins(db_session_factory, "g-6"), "the new workspace is pinned too"
    assert me is not None and len(me["workspaces"]) == 2, me
