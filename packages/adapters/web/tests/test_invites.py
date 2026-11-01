"""Invite links: reading one never uses it, accepting does once, and visitors stay inside."""

from __future__ import annotations

import uuid
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import httpx
from daimon.adapters.web.invites import issue_link
from daimon.adapters.web.owner_link import make_owner_link, owner_link_engine, revoke_owner_links
from daimon.core.config import Settings
from daimon.core.stores import spend_allowances, web_audit, web_people
from daimon.core.stores import web_roles as roles_store
from daimon.core.stores.domain import WebInviteKind, WebPersonRow
from daimon.testing.turn_router import AGENT_ID, AGENT_TEXT, build_turn_router
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .chat_support import (
    Workspace,
    build_turns,
    funded_workspace,
    one_session,
    shown_trail,
    signed_in,
    sse_frames,
)
from .conftest import PUBLIC_URL, WebClientFactory, web_settings

_OWN = {"X-Daimon-Web": "1", "Origin": PUBLIC_URL}
_COOKIE = "__Host-daimon_web"


async def _link(
    factory: async_sessionmaker[AsyncSession],
    ws: Workspace,
    *,
    kind: WebInviteKind = "invite",
    agents: tuple[str, ...] = (AGENT_ID,),
    days: int = 7,
    person_id: uuid.UUID | None = None,
) -> str:
    async with factory.begin() as db:
        _, token = await issue_link(
            db,
            tenant_id=ws.tenant_id,
            kind=kind,
            expires_at=datetime.now(UTC) + timedelta(days=days),
            email="guest@example.com",
            credit_usd=Decimal(5),
            agent_ids=agents,
            person_id=person_id,
            created_by_account_id=ws.person.account_id,
        )
    return token


def _client(
    web_client: WebClientFactory,
    factory: async_sessionmaker[AsyncSession],
    ws: Workspace,
    tmp_path: Path,
) -> AbstractAsyncContextManager[httpx.AsyncClient]:
    turns = build_turns(factory, build_turn_router(str(ws.tenant_id)), tmp_path)
    return web_client(web_settings(ws.tenant_id), sessionmaker=factory, turns=turns)


async def test_reading_an_invite_leaves_it_usable_and_accepting_uses_it_once(
    web_client: WebClientFactory,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    ws = await funded_workspace(db_session_factory)
    token = await _link(db_session_factory, ws)

    async with _client(web_client, db_session_factory, ws, tmp_path) as client:
        client.headers.update(_OWN)
        first = await client.get(f"/api/invites/{token}")
        second = await client.get(f"/api/invites/{token}")
        accepted = await client.post(f"/api/invites/{token}/accept")
        me = (await client.get("/api/me")).json()
        agents = (await client.get("/api/chat/agents")).json()
        again = await client.post(f"/api/invites/{token}/accept")
        after = await client.get(f"/api/invites/{token}")

    assert first.status_code == second.status_code == 200, "reading never uses the link"
    preview = first.json()
    assert (preview["from"], preview["email"], preview["credit_usd"]) == (
        "Ada",
        "guest@example.com",
        5.0,
    ), "the landing shows who sent it, to whom and the credit"
    assert [a["id"] for a in preview["agents"]] == [AGENT_ID], "the invite's agents are shown"
    assert accepted.json() == {"ok": True}, accepted.text
    assert (me["role"], me["invited_by"]) == ("visitor", "Ada"), "accepting signs a visitor in"
    assert (me["credit"]["left_usd"], me["credit"]["window"]) == (5.0, "total"), (
        "the invite's credit governs them"
    )
    assert [a["id"] for a in agents] == [AGENT_ID], "the visitor reaches the invited agent"
    assert again.status_code == 404, "a used link cannot be accepted again"
    assert after.status_code == 404, "a used link no longer previews"
    async with db_session_factory() as db:
        [allowance] = await spend_allowances.list_for_tenant(db, tenant_id=ws.tenant_id)
    assert (allowance.subject_kind, allowance.window) == ("invite", "total"), (
        "a visitor's credit is an allowance on their invite, for good"
    )


async def test_an_expired_invite_is_refused(
    web_client: WebClientFactory,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    ws = await funded_workspace(db_session_factory)
    token = await _link(db_session_factory, ws, days=-1)

    async with _client(web_client, db_session_factory, ws, tmp_path) as client:
        client.headers.update(_OWN)
        read = await client.get(f"/api/invites/{token}")
        accepted = await client.post(f"/api/invites/{token}/accept")

    assert read.status_code == 404, "an expired link does not preview"
    assert accepted.status_code == 404, "an expired link cannot be accepted"
    assert _COOKIE not in accepted.cookies, "no session comes of it"


async def test_a_visitor_sees_only_the_agents_they_were_invited_to(
    web_client: WebClientFactory,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    ws = await funded_workspace(db_session_factory)
    token = await _link(db_session_factory, ws, agents=("agent_elsewhere",))

    async with _client(web_client, db_session_factory, ws, tmp_path) as client:
        client.headers.update(_OWN)
        await client.post(f"/api/invites/{token}/accept")
        agents = (await client.get("/api/chat/agents")).json()
        opened = await client.post("/api/chat/sessions", json={"agent_id": AGENT_ID})

    assert agents == [], "an agent open to every member stays closed to a visitor"
    assert opened.status_code == 404, "and no chat can be started with it"


async def test_a_sign_in_link_brings_a_visitor_back(
    web_client: WebClientFactory,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    ws = await funded_workspace(db_session_factory)
    token = await _link(db_session_factory, ws)
    admin = await signed_in(db_session_factory, ws.person)
    async with db_session_factory.begin() as db:
        await roles_store.set_workspace_role(
            db,
            tenant_id=ws.tenant_id,
            account_id=ws.person.account_id,
            role="admin",
            granted_by_account_id=None,
        )

    async with _client(web_client, db_session_factory, ws, tmp_path) as client:
        client.headers.update(_OWN)
        await client.post(f"/api/invites/{token}/accept")
        client.cookies.clear()
        async with db_session_factory() as db:
            people = await web_people.list_people(db, tenant_id=ws.tenant_id)
        visitor = next(p for p in people if p.kind == "visitor")
        client.cookies.set(_COOKIE, admin, domain="web.example.com")
        member = await client.post(f"/api/admin/people/{ws.person.id}/link")
        made = await client.post(f"/api/admin/people/{visitor.id}/link")
        client.cookies.clear()
        link = made.json()["link"]
        back = link.rsplit("/", 1)[1]
        preview = await client.get(f"/api/invites/{back}")
        accepted = await client.post(f"/api/invites/{back}/accept")
        me = (await client.get("/api/me")).json()

    assert member.status_code == 404, "only a visitor gets a sign-in link"
    assert link.startswith(f"{PUBLIC_URL}/invite/"), "the link opens the invite page"
    assert [a["id"] for a in preview.json()["agents"]] == [AGENT_ID], "it shows their agents"
    assert accepted.status_code == 200, accepted.text
    assert me["id"] == str(visitor.id), "the link signs the same visitor in"
    trail = await shown_trail(db_session_factory, ws.tenant_id)
    assert ("Used sign-in link", visitor.display_name, None) in trail, (
        "using the link is audited against the visitor"
    )


async def _visitor_and_admin(
    client: httpx.AsyncClient,
    factory: async_sessionmaker[AsyncSession],
    ws: Workspace,
    invite: str,
) -> tuple[WebPersonRow, str]:
    """A visitor who accepted `invite`, and the cookie of Ada, the workspace's admin."""
    admin = await signed_in(factory, ws.person)
    async with factory.begin() as db:
        await roles_store.set_workspace_role(
            db,
            tenant_id=ws.tenant_id,
            account_id=ws.person.account_id,
            role="admin",
            granted_by_account_id=None,
        )
    await client.post(f"/api/invites/{invite}/accept")
    client.cookies.clear()
    async with factory() as db:
        people = await web_people.list_people(db, tenant_id=ws.tenant_id)
    return next(p for p in people if p.kind == "visitor"), admin


async def test_a_revoked_sign_in_link_no_longer_opens(
    web_client: WebClientFactory,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    ws = await funded_workspace(db_session_factory)
    async with _client(web_client, db_session_factory, ws, tmp_path) as client:
        client.headers.update(_OWN)
        visitor, admin = await _visitor_and_admin(
            client, db_session_factory, ws, await _link(db_session_factory, ws)
        )
        client.cookies.set(_COOKIE, admin, domain="web.example.com")
        none_yet = await client.delete(f"/api/admin/people/{visitor.id}/link")
        link = (await client.post(f"/api/admin/people/{visitor.id}/link")).json()["link"]
        revoked = await client.delete(f"/api/admin/people/{visitor.id}/link")
        client.cookies.clear()
        token = link.rsplit("/", 1)[1]
        preview, accepted = (
            await client.get(f"/api/invites/{token}"),
            await client.post(f"/api/invites/{token}/accept"),
        )

    assert none_yet.status_code == 404, "there is nothing to revoke before a link exists"
    assert revoked.json() == {"ok": True}, revoked.text
    assert (preview.status_code, accepted.status_code) == (404, 404), "the link is dead"
    trail = await shown_trail(db_session_factory, ws.tenant_id)
    assert [a for a, _, _ in trail] == [
        "Accepted invite",
        "Created sign-in link",
        "Revoked sign-in link",
    ]


async def test_suspending_a_person_revokes_their_open_sign_in_links(
    web_client: WebClientFactory,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    ws = await funded_workspace(db_session_factory)
    async with _client(web_client, db_session_factory, ws, tmp_path) as client:
        client.headers.update(_OWN)
        visitor, admin = await _visitor_and_admin(
            client, db_session_factory, ws, await _link(db_session_factory, ws)
        )
        client.cookies.set(_COOKIE, admin, domain="web.example.com")
        link = (await client.post(f"/api/admin/people/{visitor.id}/link")).json()["link"]
        await client.patch(f"/api/admin/people/{visitor.id}", json={"status": "suspended"})
        await client.patch(f"/api/admin/people/{visitor.id}", json={"status": "active"})
        client.cookies.clear()
        preview = await client.get(f"/api/invites/{link.rsplit('/', 1)[1]}")

    assert preview.status_code == 404, "restoring them does not bring back the old link"


async def test_a_revoked_owner_link_no_longer_works(
    web_client: WebClientFactory,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    ws = await funded_workspace(db_session_factory)
    token = await _link(db_session_factory, ws, kind="owner", agents=())
    session = await signed_in(db_session_factory, ws.person)

    revoked = await revoke_owner_links(
        db_session_factory, tenant_id=ws.tenant_id, now=datetime.now(UTC)
    )
    again = await revoke_owner_links(
        db_session_factory, tenant_id=ws.tenant_id, now=datetime.now(UTC)
    )
    async with _client(web_client, db_session_factory, ws, tmp_path) as client:
        client.headers.update(_OWN)
        client.cookies.set(_COOKIE, session, domain="web.example.com")
        accepted = await client.post(f"/api/invites/{token}/accept")

    assert (revoked, again) == (1, 0), "the unopened link is revoked once"
    assert accepted.status_code == 404, "and can no longer make anyone an owner"


async def test_an_owner_link_needs_a_signed_in_person(
    web_client: WebClientFactory,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    ws = await funded_workspace(db_session_factory)
    token = await _link(db_session_factory, ws, kind="owner", agents=())
    session = await signed_in(db_session_factory, ws.person)

    async with _client(web_client, db_session_factory, ws, tmp_path) as client:
        client.headers.update(_OWN)
        anonymous = await client.post(f"/api/invites/{token}/accept")
        client.cookies.set(_COOKIE, session, domain="web.example.com")
        accepted = await client.post(f"/api/invites/{token}/accept")
        me = (await client.get("/api/me")).json()

    assert anonymous.status_code == 401, "an owner link is for someone already signed in"
    assert accepted.status_code == 200, accepted.text
    assert me["role"] == "owner", "accepting makes them the owner"


async def test_a_visitor_chats_with_the_invited_agent(
    web_client: WebClientFactory,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    ws = await funded_workspace(db_session_factory)
    token = await _link(db_session_factory, ws)
    turns = build_turns(db_session_factory, build_turn_router(str(ws.tenant_id)), tmp_path)

    with one_session():
        async with web_client(
            web_settings(ws.tenant_id), sessionmaker=db_session_factory, turns=turns
        ) as client:
            client.headers.update(_OWN)
            await client.post(f"/api/invites/{token}/accept")
            sid = (await client.post("/api/chat/sessions", json={"agent_id": AGENT_ID})).json()[
                "id"
            ]
            res = await client.post(
                f"/api/chat/sessions/{sid}/messages",
                json={"id": str(uuid.uuid4()), "content": "hello", "attachments": []},
            )
            await turns.drain(10)

    done = sse_frames(res.text)[-1][1]
    assert done["type"] == "done" and done["message"]["content"] == AGENT_TEXT, (
        f"a visitor's turn runs, held to the web chat: {done}"
    )


async def test_a_visitor_cannot_take_an_owner_link_and_it_stays_usable(
    web_client: WebClientFactory,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    ws = await funded_workspace(db_session_factory)
    invite = await _link(db_session_factory, ws)
    owner_link = await _link(db_session_factory, ws, kind="owner", agents=())
    member = await signed_in(db_session_factory, ws.person)

    async with _client(web_client, db_session_factory, ws, tmp_path) as client:
        client.headers.update(_OWN)
        await client.post(f"/api/invites/{invite}/accept")
        refused = await client.post(f"/api/invites/{owner_link}/accept")
        visitor_me = (await client.get("/api/me")).json()
        client.cookies.clear()
        client.cookies.set(_COOKIE, member, domain="web.example.com")
        accepted = await client.post(f"/api/invites/{owner_link}/accept")

    assert refused.status_code == 403, "a visitor is never made an owner"
    assert visitor_me["role"] == "visitor", "and stays a visitor"
    assert accepted.status_code == 200, "the refused attempt did not use up the link"


async def test_the_owner_link_command_makes_a_link_that_grants_owner(
    web_client: WebClientFactory,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    ws = await funded_workspace(db_session_factory)
    web = web_settings(ws.tenant_id)
    settings = Settings(
        _env_file=None,  # pyright: ignore[reportCallIssue]
        database={"url": "postgresql+asyncpg://u:p@h/db"},  # pyright: ignore[reportArgumentType]
        web=web.model_dump(),  # pyright: ignore[reportArgumentType]
    )
    link = await make_owner_link(
        db_session_factory, settings, tenant_id=ws.tenant_id, days=1, now=datetime.now(UTC)
    )
    session = await signed_in(db_session_factory, ws.person)

    async with _client(web_client, db_session_factory, ws, tmp_path) as client:
        client.headers.update(_OWN)
        client.cookies.set(_COOKIE, session, domain="web.example.com")
        accepted = await client.post(f"/api/invites/{link.rsplit('/', 1)[1]}/accept")
        me = (await client.get("/api/me")).json()
    async with db_session_factory() as db:
        trail = await web_audit.list_recent(db, tenant_id=ws.tenant_id)

    assert link.startswith(f"{PUBLIC_URL}/invite/"), "the command prints an invite page link"
    assert accepted.status_code == 200 and me["role"] == "owner", accepted.text
    assert [a.action for a in trail] == ["Accepted owner link", "Created owner link"], (
        "making and using the link are both audited"
    )


async def test_the_owner_link_command_builds_its_engine_within_the_session_fence_minimum() -> None:
    """A pool under four connections is refused at build time, which crashed the command."""
    settings = Settings(
        _env_file=None,  # pyright: ignore[reportCallIssue]
        database={"url": "postgresql+asyncpg://u:p@h/db"},  # pyright: ignore[reportArgumentType]
    )
    engine = owner_link_engine(settings)
    await engine.dispose()
    assert engine.pool.status(), "the engine was built, not refused for a pool under four"
