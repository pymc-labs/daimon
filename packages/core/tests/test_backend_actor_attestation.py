"""Fresh Discord authority, bound to an existing tenant and account."""

import ssl
import uuid
from unittest.mock import patch

import httpx
import pytest
from daimon.core._models import Account, Tenant
from daimon.core.channel_backend import AgentBackendRefused
from daimon.core.config import Settings
from daimon.core.native_admins import verify_backend_actor
from daimon.core.stores import identity
from daimon.testing.factories import make_platform_principal, make_tenant
from sqlalchemy import text, update

GUILD, USER, ROLE, OTHER = (str(10**17 + n) for n in range(4))
SETTINGS = Settings.model_validate_json(
    '{"database":{"url":"postgresql+asyncpg://offline:offline@localhost/offline"},"anthropic":{"api_key":"offline"},"discord":{"bot_token":"offline-bot"}}'
)


@pytest.fixture
async def actor(db_session_factory):
    async with db_session_factory.begin() as db:
        tenant = await make_tenant(db, workspace_id=GUILD)
        principal = await make_platform_principal(
            db, tenant=tenant, platform="discord", external_id=USER
        )
    return db_session_factory, tenant, principal


def payloads(*, owner=OTHER, permissions="32"):
    return {
        "": {"id": GUILD, "owner_id": owner},
        "/roles": [{"id": GUILD, "permissions": "0"}, {"id": ROLE, "permissions": permissions}],
        f"/members/{USER}": {"user": {"id": USER}, "roles": [ROLE]},
    }


def transport(data, calls, before_reply=None):
    async def respond(request):
        if before_reply is not None:
            await before_reply(request)
        calls.append(request)
        assert request.headers["authorization"] == "Bot offline-bot"
        assert request.url.host == "discord.com" and request.method == "GET"
        assert request.extensions["timeout"]["read"] == 5
        return httpx.Response(
            200, json=data[request.url.path.removeprefix(f"/api/v10/guilds/{GUILD}")]
        )

    return httpx.MockTransport(respond)


async def read(actor, data, before_reply=None):
    sm, tenant, _ = actor
    calls = []
    async with httpx.AsyncClient(transport=transport(data, calls, before_reply)) as client:
        result = await verify_backend_actor(
            sm, tenant_id=tenant.id, platform_user_id=USER, settings=SETTINGS, http_client=client
        )
    return result, calls


@pytest.mark.parametrize("owner,permissions", [(USER, "0"), (OTHER, "8"), (OTHER, "32")])
async def test_attests_current_authority_without_promoting_or_writing(actor, owner, permissions):
    sm, tenant, principal = actor
    data = payloads(owner=owner, permissions=permissions)

    async def facts():
        async with sm() as db:
            return [
                list((await db.execute(text(f"SELECT * FROM {table} ORDER BY 1"))).all())
                for table in ("accounts", "platform_principals", "security_audit_events")
            ]

    before = await facts()
    result, calls = await read(actor, data)
    assert result.tenant_id == tenant.id and result.account_id == principal.account_id
    assert result.subject.is_admin and result.subject.platform_user_id == USER
    assert len(calls) == 3 and await facts() == before


async def test_default_producer_is_bounded_and_does_not_inherit_environment(actor, monkeypatch):
    sm, tenant, _ = actor
    monkeypatch.setenv("HTTPS_PROXY", "http://must-not-be-used.invalid")
    monkeypatch.setenv("DISCORD_TOKEN", "must-not-be-used")
    calls, real_client = [], httpx.AsyncClient

    def build(**kwargs):
        assert kwargs["trust_env"] is False and isinstance(kwargs["verify"], ssl.SSLContext)
        assert kwargs["verify"].keylog_filename is None
        return real_client(transport=transport(payloads(), calls), **kwargs)

    with patch("daimon.core.native_admins.httpx.AsyncClient", side_effect=build):
        result = await verify_backend_actor(
            sm, tenant_id=tenant.id, platform_user_id=USER, settings=SETTINGS
        )
    assert result.subject.is_admin and len(calls) == 3


@pytest.mark.parametrize("keylog", ["SSLKEYLOGFILE", "empty", "sSlKeYlOgFiLe"])
async def test_default_client_refuses_keylogging_before_ssl_or_file_creation(
    actor, monkeypatch, tmp_path, keylog
):
    sm, tenant, _ = actor
    invented = tmp_path / "tls-secrets-never-written.log"
    monkeypatch.setenv(
        "SSLKEYLOGFILE" if keylog == "empty" else keylog, "" if keylog == "empty" else str(invented)
    )
    real_client, contexts = httpx.AsyncClient, []

    def build(**kwargs):
        client = real_client(**kwargs)
        contexts.append(client._transport._pool._ssl_context.keylog_filename)

        async def offline_get(url, **request_kwargs):
            path = httpx.URL(url).path.removeprefix(f"/api/v10/guilds/{GUILD}")
            return httpx.Response(200, json=payloads()[path], request=httpx.Request("GET", url))

        client.get = offline_get
        return client

    with (
        patch("daimon.core.native_admins.httpx.AsyncClient", side_effect=build),
        pytest.raises(AgentBackendRefused, match="admin attestation unavailable"),
    ):
        await verify_backend_actor(
            sm, tenant_id=tenant.id, platform_user_id=USER, settings=SETTINGS
        )
    assert contexts == [] and not invented.exists()


@pytest.mark.parametrize(
    "path,value",
    [
        (("/roles", 1, "permissions"), "0"),
        (("/roles", 1, "permissions"), "-32"),
        (("/roles", 1, "permissions"), True),
        (("/roles", 1, "permissions"), str(2**64)),
        (("", "id"), OTHER),
        (("", "owner_id"), None),
        ((f"/members/{USER}", "user", "id"), OTHER),
        ((f"/members/{USER}", "roles"), [OTHER]),
        ((f"/members/{USER}", "roles"), [ROLE, ROLE]),
        ((f"/members/{USER}", "roles"), ROLE),
        (("/roles",), [{"id": ROLE, "permissions": "32"}] * 2),
        (("/roles",), {"id": ROLE, "permissions": "32"}),
    ],
)
async def test_unknown_or_malformed_facts_refuse(actor, path, value):
    data = payloads()
    target = data
    for part in path[:-1]:
        target = target[part]
    target[path[-1]] = value
    with pytest.raises(AgentBackendRefused):
        await read(actor, data)


@pytest.mark.parametrize("failure", [403, 404, 429, 500, "timeout", "invalid_json", "redirect"])
async def test_http_failures_refuse_without_leaking_token(actor, failure):
    sm, tenant, _ = actor

    def respond(request):
        if failure == "timeout":
            raise httpx.ReadTimeout("offline-bot", request=request)
        if failure == "invalid_json":
            return httpx.Response(200, content=b"not json")
        if failure == "redirect":
            return httpx.Response(302, headers={"Location": "https://foreign.invalid"})
        return httpx.Response(failure, json={"message": "offline-bot"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(AgentBackendRefused, match="admin attestation unavailable") as error:
            await verify_backend_actor(
                sm,
                tenant_id=tenant.id,
                platform_user_id=USER,
                settings=SETTINGS,
                http_client=client,
            )
    assert "offline-bot" not in str(error.value)


@pytest.mark.parametrize(
    "change",
    [
        "unknown_tenant",
        "unbound_user",
        "foreign_account",
        "unknown_platform",
        "no_settings",
        "invalid_id",
    ],
)
async def test_unbound_identity_refuses_before_http_without_creating_state(actor, change):
    sm, tenant, principal = actor
    if change in ("foreign_account", "unknown_platform"):
        async with sm.begin() as db:
            if change == "foreign_account":
                other = await make_tenant(db, workspace_id=OTHER)
                await db.execute(
                    update(Account)
                    .where(Account.id == principal.account_id)
                    .values(tenant_id=other.id)
                )
            if change == "unknown_platform":
                await db.execute(
                    update(Tenant).where(Tenant.id == tenant.id).values(platform="slack")
                )
    calls = []
    async with httpx.AsyncClient(transport=transport(payloads(), calls)) as client:
        with pytest.raises(AgentBackendRefused):
            await verify_backend_actor(
                sm,
                tenant_id=uuid.uuid4() if change == "unknown_tenant" else tenant.id,
                platform_user_id=OTHER
                if change == "unbound_user"
                else "not-an-id"
                if change == "invalid_id"
                else USER,
                settings=Settings.model_validate(SETTINGS.model_dump() | {"discord": None})
                if change == "no_settings"
                else SETTINGS,
                http_client=client,
            )
    assert calls == []


async def test_rechecks_permission_and_principal_every_call(actor):
    sm, _, principal = actor
    await read(actor, payloads())
    with pytest.raises(AgentBackendRefused, match="admin required"):
        await read(actor, payloads(permissions="0"))

    async def revoke(request):
        if request.url.path.endswith(USER):
            async with sm.begin() as db:
                await identity.delete_for_principal(db, principal_id=principal.id, kind="platform")

    with pytest.raises(AgentBackendRefused, match="actor not found"):
        await read(actor, payloads(), before_reply=revoke)


async def test_grant_is_not_authority_for_long_job_or_unspecified_policy(actor):
    from daimon.core.admin_actions import DelegatedAction
    from daimon.core.stores.daimon_admins import change_grant

    sm, tenant, _ = actor
    async with sm.begin() as db:
        await change_grant(
            db,
            tenant_id=tenant.id,
            platform="discord",
            subject_kind="user",
            subject_id=USER,
            actor_user_id=OTHER,
            is_platform_admin=True,
        )
    async with httpx.AsyncClient(transport=transport(payloads(permissions="0"), [])) as client:
        with pytest.raises(AgentBackendRefused):
            await verify_backend_actor(
                sm,
                tenant_id=tenant.id,
                platform_user_id=USER,
                settings=SETTINGS,
                http_client=client,
            )
        allowed = await verify_backend_actor(
            sm,
            tenant_id=tenant.id,
            platform_user_id=USER,
            settings=SETTINGS,
            http_client=client,
            action=DelegatedAction.AGENT_SETTINGS,
        )
    assert allowed.subject.is_admin
