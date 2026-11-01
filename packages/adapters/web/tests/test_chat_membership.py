"""Chat-backed people on the web: access follows the workspace's member list, and apps get
a token by exchange for a chat sign-in."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from daimon.adapters.web.cookies import new_session_token, token_sha256
from daimon.core.config import EmbeddedAppSettings, EmbeddedNavItem, Settings
from daimon.core.stores import web_sessions as sessions_store
from daimon.core.stores import workspace_members as members_store
from daimon.core.stores.domain import WebPersonRow
from daimon.testing.factories import (
    make_account,
    make_platform_principal,
    make_tenant,
    make_web_person,
)
from daimon.testing.oidc import FakeOidcProvider
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .conftest import ISSUER, PUBLIC_URL, WebClientFactory, web_settings

_OWN = {"X-Daimon-Web": "1", "Origin": PUBLIC_URL}
_NOW = datetime(2030, 1, 1, 12, tzinfo=UTC)
_USER = "223456789012345678"
_APP = EmbeddedAppSettings(
    key="data",
    name="Data",
    icon="grid",
    origin="https://app.example.com",
    oidc_issuer=ISSUER,
    nav=(EmbeddedNavItem(label="Data", path="/data", icon="grid"),),
    identity_server="hub",
)


async def _guild_person(db: AsyncSession, guild_id: str) -> WebPersonRow:
    """A person acting as Discord user `_USER` in a new guild."""
    guild = await make_tenant(db, platform="discord", workspace_id=guild_id)
    account = await make_account(db, tenant=guild)
    await make_platform_principal(
        db, platform="discord", external_id=_USER, tenant=guild, account=account
    )
    return await make_web_person(db, tenant=guild, account=account)


async def _listing(db: AsyncSession, person: WebPersonRow, members: dict[str, bool]) -> None:
    await members_store.replace_members(
        db, tenant_id=person.tenant_id, members=members, workspace_name="G", now=_NOW
    )


async def _session(factory: async_sessionmaker[AsyncSession], *people: WebPersonRow) -> str:
    token = new_session_token()
    async with factory.begin() as db:
        await sessions_store.create_session(
            db,
            token_sha256=token_sha256(token),
            provider="discord",
            issuer="discord",
            subject=_USER,
            people=list(people),
            ttl=timedelta(days=1),
            now=_NOW,
        )
    return token


def _cookie(client: httpx.AsyncClient, token: str) -> None:
    client.cookies.set("__Host-daimon_web", token, domain="web.example.com")
    client.headers.update(_OWN)


async def test_leaving_a_workspace_closes_it_and_the_app_moves_on(
    web_client: WebClientFactory,
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    gone = await _guild_person(db_session, "111111111111111111")
    kept = await _guild_person(db_session, "222222222222222222")
    await _listing(db_session, gone, {_USER: False})
    await _listing(db_session, kept, {_USER: True})
    await db_session.commit()
    token = await _session(db_session_factory, gone, kept)
    async with web_client(
        web_settings(), sessionmaker=db_session_factory, now=lambda: _NOW
    ) as client:
        _cookie(client, token)
        before = (await client.get("/api/me")).json()
        async with db_session_factory.begin() as db:
            await members_store.delete_member(db, tenant_id=gone.tenant_id, platform_user_id=_USER)
        refused = await client.get("/api/agents")
        me = await client.get("/api/me")
        back = await client.post("/api/workspaces/switch", json={"id": str(gone.tenant_id)})

    assert len(before["workspaces"]) == 2, before
    assert {w["platform"] for w in before["workspaces"]} == {"discord"}, "the chat app it lives in"
    assert refused.status_code == 403, refused.text
    assert refused.json()["detail"] == "You're no longer in this workspace."
    assert me.status_code == 200, me.text
    assert me.json()["workspace"]["id"] == str(kept.tenant_id), "moved to where they still are"
    assert [w["id"] for w in me.json()["workspaces"]] == [str(kept.tenant_id)], (
        "the workspace they left drops out of the list"
    )
    assert me.json()["role"] == "admin", "an admin in the chat workspace is a web admin"
    assert back.status_code == 403, "switching back is refused"


async def test_someone_gone_from_every_workspace_is_refused(
    web_client: WebClientFactory,
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    gone = await _guild_person(db_session, "333333333333333333")
    await _listing(db_session, gone, {})
    await db_session.commit()
    token = await _session(db_session_factory, gone)
    async with web_client(
        web_settings(), sessionmaker=db_session_factory, now=lambda: _NOW
    ) as client:
        _cookie(client, token)
        me = await client.get("/api/me")

    assert me.status_code == 403 and me.json()["detail"] == "You're no longer in this workspace."


class _Exchanging(FakeOidcProvider):
    def __init__(self, *, refuse: bool = False) -> None:
        super().__init__(issuer=ISSUER)
        self.refuse = refuse
        self.subject_tokens: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/token" and b"token-exchange" in request.content:
            response = super().handler(request)  # records the form, answers invalid_grant
            self.subject_tokens.append(self.token_requests[-1]["subject_token"])
            if self.refuse:
                return response
            return httpx.Response(200, json={"access_token": "exchanged", "expires_in": 600})
        return super().handler(request)


def _server(name: str, issuer: str, workspaces: list[str] | None = None) -> dict[str, object]:
    server: dict[str, object] = {
        "name": name,
        "url": f"https://{name}.example.com/mcp",
        "issuer": issuer,
        "audience": name,
    }
    if workspaces is not None:
        server["workspaces"] = workspaces
    return server


def _settings(workspaces: list[str] | None = None) -> Settings:
    """Settings naming the identity servers; the runtime takes the key itself."""
    return Settings(
        _env_file=None,  # pyright: ignore[reportCallIssue]
        database={"url": "postgresql+asyncpg://u:p@h/db"},  # pyright: ignore[reportArgumentType]
        mcp_identity={  # pyright: ignore[reportArgumentType]
            "servers": [_server("hub", "daimon-test", workspaces), _server("lab", "daimon-lab")]
        },
    )


async def test_a_chat_sign_in_gets_an_app_token_by_exchange(
    web_client: WebClientFactory,
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    person = await _guild_person(db_session, "444444444444444444")
    await db_session.commit()
    token = await _session(db_session_factory, person)
    key, provider = Ed25519PrivateKey.generate(), _Exchanging()
    async with web_client(
        web_settings(apps=(_APP,)),
        sessionmaker=db_session_factory,
        now=lambda: _NOW,
        provider=provider,
        settings=_settings(),
        identity_key=key,
    ) as client:
        _cookie(client, token)
        me = await client.get("/api/me")
        minted_by_me = list(provider.subject_tokens)
        got = await client.post("/api/apps/data/token")

    assert [a["key"] for a in me.json()["apps"]] == ["data"], me.text
    assert minted_by_me == [], "showing the app mints nothing; only the token request does"
    assert got.status_code == 200 and got.json()["access_token"] == "exchanged", got.text
    assert len(provider.subject_tokens) == 1, "the stored token is reused until nearly spent"
    form = provider.token_requests[0]
    assert form["subject_token_type"] == "urn:ietf:params:oauth:token-type:jwt"
    assert form["client_id"] == "web-client", "the web client authenticates the exchange"
    claims = jwt.decode(
        provider.subject_tokens[0],
        key.public_key(),
        algorithms=["EdDSA"],
        audience=ISSUER,
        options={"verify_exp": False, "verify_iat": False},
    )
    assert (claims["iss"], claims["sub"], claims["ws"]) == (
        "daimon-test",
        f"discord:{_USER}",
        "discord:444444444444444444",
    )
    assert claims["exp"] - claims["iat"] <= 300 and "agent" not in claims


async def test_a_refused_exchange_hides_the_app_and_is_not_retried_at_once(
    web_client: WebClientFactory,
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    person = await _guild_person(db_session, "555555555555555555")
    await db_session.commit()
    token = await _session(db_session_factory, person)
    key, provider = Ed25519PrivateKey.generate(), _Exchanging(refuse=True)
    async with web_client(
        web_settings(apps=(_APP,)),
        sessionmaker=db_session_factory,
        now=lambda: _NOW,
        provider=provider,
        settings=_settings(),
        identity_key=key,
    ) as client:
        _cookie(client, token)
        before = await client.get("/api/me")
        got = await client.post("/api/apps/data/token")
        after = await client.get("/api/me")
        again = await client.post("/api/apps/data/token")

    assert [a["key"] for a in before.json()["apps"]] == ["data"], "not refused yet, so shown"
    assert got.status_code == 404 and again.status_code == 404, got.text
    assert after.json()["apps"] == [], "a refused exchange hides the app"
    assert len(provider.subject_tokens) == 1, "a refusal is remembered for a while"


async def test_no_exchange_for_a_workspace_the_identity_server_does_not_serve(
    web_client: WebClientFactory,
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A server limited to some workspaces gets no subject token for anyone else's people,
    the same limit the MCP relay applies."""
    person = await _guild_person(db_session, "666666666666666666")
    await db_session.commit()
    token = await _session(db_session_factory, person)
    provider = _Exchanging()
    async with web_client(
        web_settings(apps=(_APP,)),
        sessionmaker=db_session_factory,
        now=lambda: _NOW,
        provider=provider,
        settings=_settings(workspaces=["discord:777777777777777777"]),
        identity_key=Ed25519PrivateKey.generate(),
    ) as client:
        _cookie(client, token)
        got = await client.post("/api/apps/data/token")

    assert got.status_code == 404, got.text
    assert provider.subject_tokens == [], "nothing is signed for a workspace the server refuses"


async def test_apps_naming_different_identity_servers_never_share_a_token(
    web_client: WebClientFactory,
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Two apps trusting one provider each get a token exchanged for their own server."""
    person = await _guild_person(db_session, "888888888888888888")
    await db_session.commit()
    token = await _session(db_session_factory, person)
    key, provider = Ed25519PrivateKey.generate(), _Exchanging()
    lab = _APP.model_copy(update={"key": "lab", "identity_server": "lab"})
    async with web_client(
        web_settings(apps=(_APP, lab)),
        sessionmaker=db_session_factory,
        now=lambda: _NOW,
        provider=provider,
        settings=_settings(),
        identity_key=key,
    ) as client:
        _cookie(client, token)
        first = await client.post("/api/apps/data/token")
        second = await client.post("/api/apps/lab/token")

    assert first.status_code == 200 and second.status_code == 200, second.text
    issuers = [
        jwt.decode(t, key.public_key(), algorithms=["EdDSA"], options={"verify_signature": False})[
            "iss"
        ]
        for t in provider.subject_tokens
    ]
    assert issuers == ["daimon-test", "daimon-lab"], "the second app's token is not the first's"


async def test_a_chat_admin_is_a_web_admin_until_the_flag_clears(
    web_client: WebClientFactory,
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    person = await _guild_person(db_session, "121212121212121212")
    await _listing(db_session, person, {_USER: True})
    await db_session.commit()
    token = await _session(db_session_factory, person)
    async with web_client(
        web_settings(), sessionmaker=db_session_factory, now=lambda: _NOW
    ) as client:
        _cookie(client, token)
        admin = await client.get("/api/me")
        async with db_session_factory.begin() as db:
            await members_store.update_admin_flags(
                db, tenant_id=person.tenant_id, flags={_USER: False}, now=_NOW
            )
        member = await client.get("/api/me")
        settings = await client.get("/api/admin/settings")

    assert admin.json()["role"] == "admin", "a chat admin is a web admin"
    assert member.json()["role"] == "member", "and stops being one with the chat role"
    assert settings.status_code == 403, "admin pages close at once"


async def test_switching_into_a_workspace_the_person_left_is_refused(
    web_client: WebClientFactory,
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    kept = await _guild_person(db_session, "131313131313131313")
    gone = await _guild_person(db_session, "141414141414141414")
    await _listing(db_session, kept, {_USER: False})
    await _listing(db_session, gone, {})
    await db_session.commit()
    token = await _session(db_session_factory, kept, gone)
    async with web_client(
        web_settings(), sessionmaker=db_session_factory, now=lambda: _NOW
    ) as client:
        _cookie(client, token)
        start = (await client.get("/api/me")).json()["workspace"]["id"]
        switched = await client.post("/api/workspaces/switch", json={"id": str(gone.tenant_id)})
        after = await client.get("/api/me")

    assert start == str(kept.tenant_id), start
    assert switched.status_code == 403, switched.text
    assert after.json()["workspace"]["id"] == str(kept.tenant_id), "the session stays put"


async def test_before_the_first_full_load_the_sign_in_decides_and_event_rows_count(
    web_client: WebClientFactory,
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A join or role event can write a row before the list first loads in full: that row's
    admin flag counts, and nobody is refused for missing from a list never loaded."""
    admin = await _guild_person(db_session, "151515151515151515")
    async with db_session_factory.begin() as db:
        await members_store.upsert_member(
            db, tenant_id=admin.tenant_id, platform_user_id=_USER, is_admin=True, now=_NOW
        )
    unlisted = await _guild_person(db_session, "161616161616161616")
    await db_session.commit()
    admin_token = await _session(db_session_factory, admin)
    unlisted_token = await _session(db_session_factory, unlisted)
    async with web_client(
        web_settings(), sessionmaker=db_session_factory, now=lambda: _NOW
    ) as client:
        _cookie(client, admin_token)
        as_admin = await client.get("/api/me")
        _cookie(client, unlisted_token)
        as_unlisted = await client.get("/api/me")
        async with db_session_factory.begin() as db:
            await members_store.replace_members(
                db, tenant_id=unlisted.tenant_id, members={}, workspace_name="G", now=_NOW
            )
        after_load = await client.get("/api/me")

    assert as_admin.status_code == 200 and as_admin.json()["role"] == "admin", as_admin.text
    assert as_unlisted.status_code == 200, "an unloaded list refuses nobody"
    assert as_unlisted.json()["role"] == "member", "and makes nobody admin without a row"
    assert after_load.status_code == 403, "once loaded in full, the list decides"
