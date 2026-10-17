"""A personal GitHub link needs a matching chat identity and browser."""

from __future__ import annotations

import html
import re
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from cryptography.fernet import Fernet
from daimon.adapters.mcp.oauth_github_personal import build_personal_link_routes
from daimon.core.config import (
    AnthropicSettings,
    CryptoSettings,
    DatabaseSettings,
    GithubAppSettings,
    HubSettings,
    McpSettings,
    Settings,
)
from daimon.core.github_credentials import build_multifernet
from daimon.core.stores.github_access_requests import request_access
from daimon.core.stores.github_links import account_link_status, unlink_verified_identity
from daimon.core.stores.github_personal_links import digest, get_intent, mint_link
from daimon.testing.factories import make_account, make_platform_principal, make_tenant
from pydantic import HttpUrl, PostgresDsn, SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.applications import Starlette
from starlette.routing import Route


@pytest.mark.asyncio
async def test_personal_link_verifies_browser_and_discord_user(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    key = Fernet.generate_key().decode()
    settings = Settings(
        database=DatabaseSettings(url=PostgresDsn("postgresql+asyncpg://u:p@h/d")),
        anthropic=AnthropicSettings(api_key=SecretStr("sk-test")),
        mcp=McpSettings(jwt_secret=SecretStr("a" * 32), public_url=HttpUrl("https://mcp.test/mcp")),
        crypto=CryptoSettings(keys=(SecretStr(key),)),
        github_app=GithubAppSettings(
            app_id="42",
            app_slug="sample-app",
            private_key=SecretStr("pem"),
            client_id="github-client",
            client_secret=SecretStr("github-secret"),
        ),
        hub=HubSettings(
            discord_client_id="discord-client",
            discord_client_secret=SecretStr("discord-secret"),
        ),
    )
    async with committing_sessionmaker.begin() as session:
        tenant = await make_tenant(session, platform="discord", workspace_id="123")
        account = await make_account(session, tenant=tenant)
        await make_platform_principal(
            session,
            platform="discord",
            external_id="456",
            tenant=tenant,
            account=account,
        )
        second_tenant = await make_tenant(session, platform="discord", workspace_id="999")
        second_account = await make_account(session, tenant=second_tenant)
        await make_platform_principal(
            session,
            platform="discord",
            external_id="456",
            tenant=second_tenant,
            account=second_account,
        )
        link = await mint_link(
            session,
            tenant_id=tenant.id,
            account_id=account.id,
            platform="discord",
            platform_user_id="456",
            root_url="https://mcp.test",
        )
    actual_discord_user = "789"

    def api(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/oauth2/token":
            return httpx.Response(200, json={"access_token": "discord-access"})
        if request.url.path == "/api/users/@me":
            return httpx.Response(200, json={"id": actual_discord_user})
        if request.url.path == "/login/oauth/access_token":
            assert parse_qs(request.content.decode())["code_verifier"][0]
            return httpx.Response(
                200,
                json={
                    "access_token": "github-access",
                    "refresh_token": "github-refresh",
                    "expires_in": 28800,
                    "refresh_token_expires_in": 15897600,
                },
            )
        if request.url.path == "/user":
            return httpx.Response(200, json={"id": 17, "login": "carlos"})
        raise AssertionError(f"unexpected API call {request.url}")

    def factory() -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(api))

    start, platform_callback, github_callback = build_personal_link_routes(
        settings=settings,
        sessionmaker=committing_sessionmaker,
        fernet=build_multifernet((key,)),
        client_factory=factory,
    )
    app = Starlette(
        routes=[
            Route("/oauth/github/link/platform-callback", platform_callback),
            Route("/oauth/github/link/{token}", start),
            Route("/oauth/github/callback", github_callback),
        ]
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://mcp.test"
    ) as browser:
        opened = await browser.get(link)
        assert opened.status_code == 307
        assert urlparse(opened.headers["location"]).hostname == "discord.com"
        platform_state = parse_qs(urlparse(opened.headers["location"]).query)["state"][0]
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="https://mcp.test"
        ) as forwarded:
            refused = await forwarded.get(
                "/oauth/github/link/platform-callback",
                params={"state": platform_state, "code": "discord-code"},
            )
            assert refused.status_code == 400
        wrong_person = await browser.get(
            "/oauth/github/link/platform-callback",
            params={"state": platform_state, "code": "discord-code"},
        )
        assert wrong_person.status_code == 403
        actual_discord_user = "456"
        verified = await browser.get(
            "/oauth/github/link/platform-callback",
            params={"state": platform_state, "code": "discord-code"},
        )
        assert verified.status_code == 307
        github_state = parse_qs(urlparse(verified.headers["location"]).query)["state"][0]
        linked = await browser.get(
            "/oauth/github/callback", params={"state": github_state, "code": "github-code"}
        )
        assert linked.status_code == 200
        assert "Linked as @carlos." in _text(linked)
        repeated = await browser.get(
            "/oauth/github/callback", params={"state": github_state, "code": "github-code"}
        )
        assert repeated.status_code == 400
        async with committing_sessionmaker.begin() as session:
            expiring = await mint_link(
                session,
                tenant_id=tenant.id,
                account_id=account.id,
                platform="discord",
                platform_user_id="456",
                root_url="https://mcp.test",
            )
            old = await get_intent(
                session,
                token_hash=digest(expiring.rsplit("/", 1)[-1]),
                include_expired=True,
            )
            assert old is not None
            old.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        expired = await browser.get(expiring)
        assert "This link expired. Ask again in chat." in expired.text
        assert "Get a new link" not in expired.text
    async with committing_sessionmaker() as session:
        assert await account_link_status(session, account_id=account.id) == "carlos"
        assert await account_link_status(session, account_id=second_account.id) == "carlos"
    async with committing_sessionmaker.begin() as session:
        assert (
            await unlink_verified_identity(
                session,
                tenant_id=tenant.id,
                platform="discord",
                platform_user_id="456",
                account_id=account.id,
            )
            == 2
        )
    async with committing_sessionmaker() as session:
        assert await account_link_status(session, account_id=account.id) is None
        assert await account_link_status(session, account_id=second_account.id) is None


@pytest.mark.asyncio
async def test_personal_link_signs_in_with_slack_before_github(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    key = Fernet.generate_key().decode()
    settings = Settings(
        database=DatabaseSettings(url=PostgresDsn("postgresql+asyncpg://u:p@h/d")),
        anthropic=AnthropicSettings(api_key=SecretStr("sk-test")),
        mcp=McpSettings(jwt_secret=SecretStr("a" * 32), public_url=HttpUrl("https://mcp.test/mcp")),
        crypto=CryptoSettings(keys=(SecretStr(key),)),
        github_app=GithubAppSettings(
            app_id="42",
            app_slug="sample-app",
            private_key=SecretStr("pem"),
            client_id="github-client",
            client_secret=SecretStr("github-secret"),
        ),
        hub=HubSettings(slack_client_id="slack-client", slack_client_secret=SecretStr("secret")),
    )
    async with committing_sessionmaker.begin() as session:
        tenant = await make_tenant(session, platform="slack", workspace_id="T123")
        account = await make_account(session, tenant=tenant)
        await make_platform_principal(
            session, platform="slack", external_id="U456", tenant=tenant, account=account
        )
        link = await mint_link(
            session,
            tenant_id=tenant.id,
            account_id=account.id,
            platform="slack",
            platform_user_id="U456",
            root_url="https://mcp.test",
        )

    def api(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/openid.connect.token":
            return httpx.Response(200, json={"ok": True, "access_token": "slack-access"})
        if request.url.path == "/api/openid.connect.userInfo":
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "https://slack.com/user_id": "U456",
                    "https://slack.com/team_id": "T123",
                },
            )
        if request.url.path == "/login/oauth/access_token":
            return httpx.Response(
                200,
                json={
                    "access_token": "github-access",
                    "refresh_token": "github-refresh",
                    "expires_in": 28800,
                    "refresh_token_expires_in": 15897600,
                },
            )
        if request.url.path == "/user":
            return httpx.Response(200, json={"id": 17, "login": "carlos"})
        raise AssertionError(f"unexpected API call {request.url}")

    start, platform_callback, github_callback = build_personal_link_routes(
        settings=settings,
        sessionmaker=committing_sessionmaker,
        fernet=build_multifernet((key,)),
        client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(api)),
    )
    app = Starlette(
        routes=[
            Route("/oauth/github/link/platform-callback", platform_callback),
            Route("/oauth/github/link/{token}", start),
            Route("/oauth/github/callback", github_callback),
        ]
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://mcp.test"
    ) as browser:
        opened = await browser.get(link)
        assert opened.status_code == 307
        assert urlparse(opened.headers["location"]).hostname == "slack.com"
        assert parse_qs(urlparse(opened.headers["location"]).query)["team"] == ["T123"]
        platform_state = parse_qs(urlparse(opened.headers["location"]).query)["state"][0]
        verified = await browser.get(
            "/oauth/github/link/platform-callback",
            params={"state": platform_state, "code": "slack-code"},
        )
        assert verified.status_code == 307
        assert urlparse(verified.headers["location"]).hostname == "github.com"
        github_state = parse_qs(urlparse(verified.headers["location"]).query)["state"][0]
        linked = await browser.get(
            "/oauth/github/callback", params={"state": github_state, "code": "github-code"}
        )
        assert linked.status_code == 200
        assert "Linked as @carlos." in _text(linked)
    async with committing_sessionmaker() as session:
        assert await account_link_status(session, account_id=account.id) == "carlos"


_SELECT = "prompt=select_account"


def _synthetic_settings(key: str) -> Settings:
    return Settings(
        database=DatabaseSettings(url=PostgresDsn("postgresql+asyncpg://u:p@h/d")),
        anthropic=AnthropicSettings(api_key=SecretStr("sk-test")),
        mcp=McpSettings(jwt_secret=SecretStr("a" * 32), public_url=HttpUrl("https://mcp.test/mcp")),
        crypto=CryptoSettings(keys=(SecretStr(key),)),
        github_app=GithubAppSettings(
            app_id="42",
            app_slug="sample-app",
            private_key=SecretStr("pem"),
            client_id="github-client",
            client_secret=SecretStr("github-secret"),
        ),
        hub=HubSettings(
            discord_client_id="discord-client",
            discord_client_secret=SecretStr("discord-secret"),
            slack_client_id="slack-client",
            slack_client_secret=SecretStr("slack-secret"),
        ),
    )


async def _world(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    platform: str,
    github_ok: bool = True,
    platform_user: Callable[[], str] | None = None,
    workspace_id: str | None = None,
):
    """A tenant, a person and the three browser routes, with synthetic remote APIs."""
    key = Fernet.generate_key().decode()
    workspace = workspace_id or ("123" if platform == "discord" else "T123")
    person = "456" if platform == "discord" else "U456"
    async with sessionmaker.begin() as session:
        tenant = await make_tenant(session, platform=platform, workspace_id=workspace)
        account = await make_account(session, tenant=tenant)
        await make_platform_principal(
            session, platform=platform, external_id=person, tenant=tenant, account=account
        )

    def api(request: httpx.Request) -> httpx.Response:
        who = platform_user() if platform_user else person
        path = request.url.path
        if path == "/api/oauth2/token":
            return httpx.Response(200, json={"access_token": "platform-access"})
        if path == "/api/users/@me":
            return httpx.Response(200, json={"id": who})
        if path == "/api/openid.connect.token":
            return httpx.Response(200, json={"ok": True, "access_token": "platform-access"})
        if path == "/api/openid.connect.userInfo":
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "https://slack.com/user_id": who,
                    "https://slack.com/team_id": workspace,
                },
            )
        if path == "/login/oauth/access_token":
            if not github_ok:
                return httpx.Response(500)
            return httpx.Response(
                200,
                json={
                    "access_token": "github-access",
                    "refresh_token": "github-refresh",
                    "expires_in": 28800,
                    "refresh_token_expires_in": 15897600,
                },
            )
        if path == "/user":
            return httpx.Response(200, json={"id": 17, "login": "sam-rivera"})
        raise AssertionError(f"unexpected API call {request.url}")

    settings = _synthetic_settings(key)
    start, platform_callback, github_callback = build_personal_link_routes(
        settings=settings,
        sessionmaker=sessionmaker,
        fernet=build_multifernet((key,)),
        client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(api)),
    )
    app = Starlette(
        routes=[
            Route("/oauth/github/link/platform-callback", platform_callback),
            Route("/oauth/github/link/{token}", start),
            Route("/oauth/github/callback", github_callback),
        ]
    )

    async def new_link() -> str:
        async with sessionmaker.begin() as session:
            return await mint_link(
                session,
                tenant_id=tenant.id,
                account_id=account.id,
                platform=platform,
                platform_user_id=person,
                root_url="https://mcp.test",
            )

    def browser() -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="https://mcp.test"
        )

    return SimpleNamespace(
        tenant=tenant,
        account=account,
        person=person,
        workspace=workspace,
        new_link=new_link,
        browser=browser,
    )


async def _until_platform_verified(browser: httpx.AsyncClient, link: str) -> httpx.Response:
    opened = await browser.get(link)
    state = parse_qs(urlparse(opened.headers["location"]).query)["state"][0]
    return await browser.get(
        "/oauth/github/link/platform-callback", params={"state": state, "code": "platform-code"}
    )


def _github_state(verified: httpx.Response) -> str:
    return parse_qs(urlparse(verified.headers["location"]).query)["state"][0]


def _text(page: httpx.Response) -> str:
    """The visible text: no style block, no tags, entities decoded."""
    body = re.sub(r"<style>.*?</style>", "", page.text, flags=re.S)
    return html.unescape(re.sub(r"<[^>]+>", "", body))


def _heading(page: httpx.Response) -> str:
    match = re.search(r"<h1>(.*?)</h1>", page.text)
    assert match, page.text
    return html.unescape(match.group(1))


def _hrefs(page: httpx.Response) -> dict[str, str]:
    return {
        label.strip(): href.replace("&amp;", "&")
        for href, label in re.findall(
            r'<a class="gh-(?:primary|secondary)[^"]*" href="([^"]+)">(?:<svg.*?</svg>)?([^<]+)</a>',
            page.text,
            flags=re.S,
        )
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "platform,back_label,back_url,generic_url",
    [
        (
            "discord",
            "Back to Discord",
            "https://discord.com/channels/123",
            "https://discord.com/app",
        ),
        (
            "slack",
            "Back to Slack",
            "https://app.slack.com/client/T123",
            "https://app.slack.com/client/",
        ),
    ],
)
async def test_browser_outcomes_use_the_agreed_titles_and_buttons(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    platform: str,
    back_label: str,
    back_url: str,
    generic_url: str,
) -> None:
    other = {"user": ""}
    world = await _world(
        committing_sessionmaker,
        platform=platform,
        platform_user=lambda: other["user"] or world.person,
    )
    pages: list[httpx.Response] = []

    async with world.browser() as browser:
        # Linked, no pending request.
        verified = await _until_platform_verified(browser, await world.new_link())
        linked = await browser.get(
            "/oauth/github/callback",
            params={"state": _github_state(verified), "code": "github-code"},
        )
        assert linked.status_code == 200
        assert _heading(linked) == "GitHub linked"
        assert "Linked as @sam-rivera." in _text(linked)
        assert "Check " not in _text(linked)
        assert _hrefs(linked) == {back_label: back_url}
        pages.append(linked)

        # Someone else opened the link: stops before GitHub.
        other["user"] = "999"
        wrong_person = await _until_platform_verified(browser, await world.new_link())
        assert wrong_person.status_code == 403
        assert _heading(wrong_person) == "Not your link"
        assert (
            f"This link was made for another person. Ask in {platform.title()} for your own link."
            in _text(wrong_person)
        )
        # The identity is not verified yet, so the way back names no server.
        assert _hrefs(wrong_person) == {back_label: generic_url}
        pages.append(wrong_person)
        other["user"] = ""

        # Expired or already used link.
        used = await browser.get(await world.new_link() + "x")
        assert used.status_code == 400
        assert _heading(used) == "Link expired"
        assert "This link expired. Ask again in chat." in _text(used)
        assert _hrefs(used) == {}
        pages.append(used)

        # GitHub refused or was cancelled: a fresh link and a way back.
        verified = await _until_platform_verified(browser, await world.new_link())
        cancelled = await browser.get(
            "/oauth/github/callback", params={"state": _github_state(verified)}
        )
        assert cancelled.status_code == 200
        assert _heading(cancelled) == "GitHub wasn't linked"
        hrefs = _hrefs(cancelled)
        assert list(hrefs) == ["Try again", back_label]
        assert hrefs["Try again"].startswith("https://mcp.test/oauth/github/link/")
        assert hrefs[back_label] == back_url
        assert _SELECT not in hrefs["Try again"]
        pages.append(cancelled)

        # The same refusal before GitHub, at the platform step.
        opened = await browser.get(await world.new_link())
        state = parse_qs(urlparse(opened.headers["location"]).query)["state"][0]
        refused = await browser.get("/oauth/github/link/platform-callback", params={"state": state})
        assert refused.status_code == 200
        assert _heading(refused) == "GitHub wasn't linked"
        assert list(_hrefs(refused)) == ["Try again", back_label]
        assert _hrefs(refused)[back_label] == generic_url
        pages.append(refused)

    for page in pages:
        assert "close this tab" not in _text(page).lower()
        assert "Back to Discord" not in _text(page) or platform == "discord"


@pytest.mark.asyncio
async def test_github_connection_problem_page_keeps_its_status_and_retry(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    world = await _world(committing_sessionmaker, platform="discord", github_ok=False)
    async with world.browser() as browser:
        verified = await _until_platform_verified(browser, await world.new_link())
        down = await browser.get(
            "/oauth/github/callback",
            params={"state": _github_state(verified), "code": "github-code"},
        )
    assert down.status_code == 502
    assert _heading(down) == "Connection problem"
    assert "GitHub didn't answer. Try again in a minute." in _text(down)
    assert list(_hrefs(down)) == ["Try again"]
    assert _hrefs(down)["Try again"].startswith("https://mcp.test/oauth/github/link/")
    assert "close this tab" not in _text(down).lower()


@pytest.mark.asyncio
async def test_wrong_account_page_names_the_repo_and_reuses_the_start_route(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    world = await _world(committing_sessionmaker, platform="discord")
    async with committing_sessionmaker.begin() as session:
        await request_access(
            session,
            tenant_id=world.tenant.id,
            requester_account_id=world.account.id,
            requester_platform_user_id=world.person,
            platform="discord",
            parent_channel_id="channel",
            thread_id="thread",
            agent_id=uuid.uuid4(),
            ma_agent_id="ma-agent",
            agent_name="Atlas",
            repo_name="northwind-labs/analysis",
            requested_work=None,
            is_admin=False,
        )
    async with world.browser() as browser:
        verified = await _until_platform_verified(browser, await world.new_link())
        page = await browser.get(
            "/oauth/github/callback",
            params={"state": _github_state(verified), "code": "github-code"},
        )
        assert page.status_code == 200
        assert _heading(page) == "Wrong GitHub account"
        assert "@sam-rivera can't open northwind-labs/analysis." in _text(page)
        # Names never break at a hyphen: each sits in its own nowrap span.
        assert '<span style="white-space:nowrap">northwind-labs/analysis</span>' in page.text
        assert '<span style="white-space:nowrap">@sam-rivera</span>' in page.text
        hrefs = _hrefs(page)
        # "Cancel request" would need a new state-changing endpoint, so it is not offered.
        assert list(hrefs) == ["Use another GitHub account", "Back to Discord"]
        assert "Cancel request" not in _text(page)
        assert "close this tab" not in _text(page).lower()
        another = hrefs["Use another GitHub account"]
        assert another.startswith("https://mcp.test/oauth/github/link/")
        assert another.endswith("?select_account=1")

        # The button opens the existing start route; GitHub is then asked to show its picker.
        opened = await browser.get(another.removeprefix("https://mcp.test"))
        assert opened.status_code == 307
        state = parse_qs(urlparse(opened.headers["location"]).query)["state"][0]
        verified = await browser.get(
            "/oauth/github/link/platform-callback", params={"state": state, "code": "platform-code"}
        )
        assert _SELECT in verified.headers["location"]
        assert urlparse(verified.headers["location"]).hostname == "github.com"


@pytest.mark.asyncio
async def test_plain_link_does_not_ask_github_to_choose_an_account(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    world = await _world(committing_sessionmaker, platform="discord")
    async with world.browser() as browser:
        # A flagged start, then a plain one: the flag must not stick to the browser.
        flagged = await browser.get(await world.new_link() + "?select_account=1")
        assert flagged.status_code == 307
        verified = await _until_platform_verified(browser, await world.new_link())
    assert _SELECT not in verified.headers["location"]


def test_names_are_escaped_before_they_are_wrapped() -> None:
    from daimon.adapters.mcp.oauth_github_personal import (
        _nowrap,  # pyright: ignore[reportPrivateUsage]
    )

    wrapped = _nowrap(
        "<b>@sam-rivera</b> can't open northwind-labs/analysis & northwind-labs/*.",
        ("northwind-labs/analysis", "northwind-labs/*", "@sam-rivera"),
    )
    assert "<b>" not in wrapped and "&lt;b&gt;" in wrapped and "&amp;" in wrapped
    for name in ("northwind-labs/analysis", "northwind-labs/*", "@sam-rivera"):
        assert f'<span style="white-space:nowrap">{name}</span>' in wrapped
    assert wrapped.count("<span") == 3


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "platform,back_url",
    [
        ("discord", "https://discord.com/app"),
        ("slack", "https://app.slack.com/client/"),
    ],
)
async def test_expired_link_with_a_known_platform_offers_the_way_back(
    committing_sessionmaker: async_sessionmaker[AsyncSession], platform: str, back_url: str
) -> None:
    world = await _world(committing_sessionmaker, platform=platform)
    async with world.browser() as browser:
        link = await world.new_link()
        assert (await browser.get(link)).status_code == 307
        # Opening a link that was already started finds its row, so the platform is known.
        again = await browser.get(link)
    assert again.status_code == 400
    assert _heading(again) == "Link expired"
    assert _hrefs(again) == {f"Back to {platform.title()}": back_url}
    assert 'class="gh-primary"' in again.text
    assert "close this tab" not in _text(again).lower()


@pytest.mark.asyncio
async def test_back_button_is_an_outline_button_with_a_44px_tap_target(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    world = await _world(committing_sessionmaker, platform="discord")
    async with world.browser() as browser:
        opened = await browser.get(await world.new_link())
        state = parse_qs(urlparse(opened.headers["location"]).query)["state"][0]
        refused = await browser.get("/oauth/github/link/platform-callback", params={"state": state})
    assert 'class="gh-primary"' in refused.text  # Try again
    assert re.search(r'<a class="gh-secondary" href="https://discord.com/app">', refused.text)
    css = refused.text.split(".gh-secondary{", 1)[1].split("}", 1)[0]
    for rule in (
        "min-height:44px",
        "gap:8px",
        "display:inline-flex",
        "background:#fff",
        "border:1px solid",
    ):
        assert rule in css
    assert "web-brand-link" not in refused.text


def test_a_name_that_looks_like_an_entity_cannot_break_the_escaping() -> None:
    from daimon.adapters.mcp.oauth_github_personal import (
        _nowrap,  # pyright: ignore[reportPrivateUsage]
    )

    wrapped = _nowrap("@x27 can't open acme/a-b.", ("@x27", "acme/a-b"))
    assert wrapped == (
        '<span style="white-space:nowrap">@x27</span> can&#x27;t open '
        '<span style="white-space:nowrap">acme/a-b</span>.'
    )
    bare = _nowrap("x27 & amp can't", ("x27", "amp"))
    assert "&amp;" in bare and "&#x27;" in bare
    assert bare.count("<span") == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("platform", ["discord", "slack"])
async def test_pages_without_a_verified_browser_never_carry_the_workspace_or_channel(
    committing_sessionmaker: async_sessionmaker[AsyncSession], platform: str
) -> None:
    workspace = "778899123456"
    other = {"user": ""}
    world = await _world(
        committing_sessionmaker,
        platform=platform,
        workspace_id=workspace,
        platform_user=lambda: other["user"] or world.person,
    )
    async with committing_sessionmaker.begin() as session:
        await request_access(
            session,
            tenant_id=world.tenant.id,
            requester_account_id=world.account.id,
            requester_platform_user_id=world.person,
            platform="discord" if platform == "discord" else "slack",
            parent_channel_id="channel-554433",
            thread_id="thread-998877",
            agent_id=uuid.uuid4(),
            ma_agent_id="ma-agent",
            agent_name="Atlas",
            repo_name="northwind-labs/analysis",
            requested_work=None,
            is_admin=False,
        )
    pages: dict[str, httpx.Response] = {}
    async with world.browser() as browser:
        link = await world.new_link()
        assert (await browser.get(link)).status_code == 307
        pages["expired: started link opened again"] = await browser.get(link)

        # A browser without the link's cookie, at the platform and at the GitHub step.
        link = await world.new_link()
        opened = await browser.get(link)
        state = parse_qs(urlparse(opened.headers["location"]).query)["state"][0]
        async with world.browser() as stranger:
            pages["cookie mismatch: platform step"] = await stranger.get(
                "/oauth/github/link/platform-callback",
                params={"state": state, "code": "platform-code"},
            )
        verified = await browser.get(
            "/oauth/github/link/platform-callback",
            params={"state": state, "code": "platform-code"},
        )
        async with world.browser() as stranger:
            pages["cookie mismatch: github step"] = await stranger.get(
                "/oauth/github/callback",
                params={"state": _github_state(verified), "code": "github-code"},
            )

        # The platform step before the chat identity is verified.
        other["user"] = "999"
        pages["not your link"] = await _until_platform_verified(browser, await world.new_link())
        other["user"] = ""
        opened = await browser.get(await world.new_link())
        state = parse_qs(urlparse(opened.headers["location"]).query)["state"][0]
        pages["refused at the platform"] = await browser.get(
            "/oauth/github/link/platform-callback", params={"state": state}
        )

    generic = {"discord": "https://discord.com/app", "slack": "https://app.slack.com/client/"}[
        platform
    ]
    for name, page in pages.items():
        for secret in (workspace, "channel-554433", "thread-998877"):
            assert secret not in page.text, f"{name} leaks {secret}"
        back = [href for label, href in _hrefs(page).items() if label.startswith("Back to")]
        assert back in ([generic], []), name
    assert pages["expired: started link opened again"].status_code == 400
    assert _hrefs(pages["expired: started link opened again"]) == {
        f"Back to {platform.title()}": generic
    }
    assert pages["cookie mismatch: platform step"].status_code == 400
    assert pages["cookie mismatch: github step"].status_code == 400
    assert pages["not your link"].status_code == 403


@pytest.mark.asyncio
async def test_workspace_link_is_kept_after_the_browser_and_identity_are_verified(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    world = await _world(committing_sessionmaker, platform="discord", workspace_id="778899123456")
    async with world.browser() as browser:
        verified = await _until_platform_verified(browser, await world.new_link())
        linked = await browser.get(
            "/oauth/github/callback",
            params={"state": _github_state(verified), "code": "github-code"},
        )
    assert _hrefs(linked) == {"Back to Discord": "https://discord.com/channels/778899123456"}
