"""Connecting more chat accounts over HTTP: one browser reaches every account's workspaces."""

from __future__ import annotations

from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
from daimon.testing.factories import make_tenant, make_web_person
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .chat_support import signed_in
from .conftest import (
    PUBLIC_URL,
    WebClientFactory,
    chat_settings,
    fake_discord,
    fake_slack,
    web_settings,
)

_SESSION = "__Host-daimon_web"
_OWN = {"X-Daimon-Web": "1", "Origin": PUBLIC_URL}
_PROVIDER = fake_discord(
    {"id": "d1", "username": "ada"},
    [("g1", "Acme")],
    fallback=fake_slack(team=("T1", "Beta"), user_id="U1"),
)


async def _walk(client: httpx.AsyncClient, provider: str, *, connect: bool) -> httpx.Response:
    """Start a sign-in or a connect and come back from the provider with a code."""
    if connect:
        start = await client.post(f"/auth/{provider}/connect", json={}, headers=_OWN)
        assert start.status_code == 200, start.text
        authorize: str = start.json()["url"]
    else:
        start = await client.get(f"/auth/{provider}/start")
        authorize = start.headers["location"]
    state = parse_qs(urlsplit(authorize).query)["state"][0]
    return await client.get(f"/auth/{provider}/callback", params={"code": "c", "state": state})


async def _me(client: httpx.AsyncClient) -> dict[str, Any]:
    res = await client.get("/api/me")
    assert res.status_code == 200, res.text
    return res.json()


async def _installed(db_session: AsyncSession) -> None:
    await make_tenant(db_session, platform="discord", workspace_id="g1")
    await make_tenant(db_session, platform="slack", workspace_id="T1")
    await db_session.commit()


async def test_a_connected_account_adds_workspaces_and_is_remembered_on_another_device(
    web_client: WebClientFactory,
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _installed(db_session)
    async with web_client(
        web_settings(),
        sessionmaker=db_session_factory,
        provider=_PROVIDER,
        settings=chat_settings(),
    ) as client:
        await _walk(client, "discord", connect=False)
        before = await _me(client)
        await _walk(client, "slack", connect=True)
        after = await _me(client)
    async with web_client(
        web_settings(),
        sessionmaker=db_session_factory,
        provider=_PROVIDER,
        settings=chat_settings(),
    ) as other_device:
        await _walk(other_device, "slack", connect=False)
        remembered = await _me(other_device)

    assert [w["platform"] for w in before["workspaces"]] == ["discord"], "Discord alone at first"
    assert sorted(w["platform"] for w in after["workspaces"]) == ["discord", "slack"], (
        "connecting Slack adds its workspace"
    )
    assert sorted(a["provider"] for a in after["accounts"]) == ["discord", "slack"], after
    assert sorted(w["platform"] for w in remembered["workspaces"]) == ["discord", "slack"], (
        "signing in with Slack alone on another device shows the Discord workspace too"
    )


async def test_a_connect_flow_from_another_session_is_refused(
    web_client: WebClientFactory,
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session, platform="discord", workspace_id="g1")
    await make_tenant(db_session, platform="slack", workspace_id="T1")
    ada = await make_web_person(db_session, tenant=tenant, email="ada@example.com")
    bea = await make_web_person(db_session, tenant=tenant, email="bea@example.com")
    await db_session.commit()
    ada_token = await signed_in(db_session_factory, ada)
    bea_token = await signed_in(db_session_factory, bea)
    async with web_client(
        web_settings(),
        sessionmaker=db_session_factory,
        provider=_PROVIDER,
        settings=chat_settings(),
    ) as client:
        client.cookies.set(_SESSION, ada_token, domain="web.example.com")
        start = await client.post("/auth/slack/connect", json={}, headers=_OWN)
        url: str = start.json()["url"]
        state = parse_qs(urlsplit(url).query)["state"][0]
        client.cookies.set(_SESSION, bea_token, domain="web.example.com")
        back = await client.get("/auth/slack/callback", params={"code": "c", "state": state})
        me = await _me(client)

    assert urlsplit(back.headers["location"]).path == "/signin", "the flow belongs to Ada's session"
    assert [w["platform"] for w in me["workspaces"]] == ["discord"], "Bea gained nothing"


async def test_disconnect_removes_an_account_but_never_the_one_signed_in_with(
    web_client: WebClientFactory,
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _installed(db_session)
    async with web_client(
        web_settings(),
        sessionmaker=db_session_factory,
        provider=_PROVIDER,
        settings=chat_settings(),
    ) as client:
        await _walk(client, "discord", connect=False)
        await _walk(client, "slack", connect=True)
        accounts = {a["provider"]: a for a in (await _me(client))["accounts"]}
        own = await client.delete(f"/api/me/accounts/{accounts['discord']['id']}", headers=_OWN)
        removed = await client.delete(f"/api/me/accounts/{accounts['slack']['id']}", headers=_OWN)
        after = await _me(client)

    assert accounts["discord"]["current"] and not accounts["slack"]["current"], accounts
    assert own.status_code == 409, "the sign-in account stays"
    assert removed.status_code == 200, removed.text
    assert [a["provider"] for a in after["accounts"]] == ["discord"], after
    assert [w["platform"] for w in after["workspaces"]] == ["discord"], "its workspace left too"
