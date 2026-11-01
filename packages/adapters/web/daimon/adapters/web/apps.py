"""Embedded apps: the person's sign-in token for an app, the app's status, an agent's panels.

An app gets the access token of the provider it trusts. A person signed in with that
provider gets their own; refresh tokens rotate, so refreshes are serialized per sign-in.
Anyone acting as a chat account gets one by token exchange, where the app allows it.
An app the person has no token for is left out of what the web app shows.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Annotated
from urllib.parse import quote

import httpx
import structlog
from cryptography.fernet import InvalidToken
from daimon.adapters.web.app_probe import AppStatus
from daimon.adapters.web.caller import WebCaller, current_caller
from daimon.adapters.web.runtime import WebRuntime
from daimon.core import web_oidc
from daimon.core.config import EmbeddedAppSettings, WebSettings, origin_of
from daimon.core.github_credentials import decrypt_token, encrypt_token
from daimon.core.stores import tenants as tenants_store
from daimon.core.stores import web_oidc_tokens as tokens_store
from daimon.core.stores import web_sessions as sessions_store
from daimon.core.stores.domain import CHAT_PLATFORMS
from daimon.core.web_identity import linked_identity
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

log = structlog.get_logger(__name__)

#: A token with less left than this is refreshed before it is handed out.
MIN_TOKEN_LEFT = timedelta(seconds=60)
#: How long the status probe waits for the app.
APP_TIMEOUT_S = 3.0
#: A refused exchange is not tried again for this long.
EXCHANGE_BACKOFF = timedelta(minutes=5)
#: How long an exchanged token is taken to last when the provider does not say.
DEFAULT_TOKEN_LIFE = timedelta(minutes=5)
_MAX_REFUSALS = 1024


@dataclass(frozen=True, slots=True)
class AppToken:
    access_token: str
    expires_at: datetime


class TokenOut(BaseModel):
    access_token: str
    expires_at: datetime


class AgentApp(BaseModel):
    """An app's views of one agent, as paths inside the app."""

    key: str
    embed: str
    page: str | None


def agent_apps(
    apps: Iterable[EmbeddedAppSettings], *, agent_id: str, server_urls: Iterable[str]
) -> list[AgentApp]:
    """Each app with an agent panel that the agent has an MCP server at, its paths filled in.

    `agent_id` is the id the agent has at MCP servers; it is URL-encoded into the templates.
    """
    origins = {origin_of(url) for url in server_urls}
    fill = quote(agent_id, safe="")
    return [
        AgentApp(
            key=app.key,
            embed=panel.embed.replace("{agent_id}", fill),
            page=None if panel.page is None else panel.page.replace("{agent_id}", fill),
        )
        for app in apps
        if (panel := app.panels.agent) is not None and app.origin in origins
    ]


def app_for(web: WebSettings, key: str) -> EmbeddedAppSettings | None:
    return next((app for app in web.apps if app.key == key), None)


async def app_token(
    runtime: WebRuntime, caller: WebCaller, app: EmbeddedAppSettings
) -> AppToken | None:
    """The caller's access token for `app`, refreshed or exchanged when nearly spent; None
    when they have none."""
    issuer = app.oidc_issuer
    if issuer is None:
        return None
    session = caller.session
    if session.provider == "oidc" and session.issuer == issuer:
        got = await _signin_token(runtime, caller, issuer)
        if got is not None:
            return got
    if app.identity_server is not None:
        return await _exchanged_token(runtime, caller, app, issuer)
    return None


async def app_available(runtime: WebRuntime, caller: WebCaller, app: EmbeddedAppSettings) -> bool:
    """Whether the web app shows `app` to the caller: never when it would open empty."""
    return app.oidc_issuer is None or await app_token(runtime, caller, app) is not None


async def available_apps(runtime: WebRuntime, caller: WebCaller) -> list[EmbeddedAppSettings]:
    return [app for app in runtime.web.apps if await app_available(runtime, caller, app)]


async def _signin_token(runtime: WebRuntime, caller: WebCaller, issuer: str) -> AppToken | None:
    """The token the caller's own sign-in brought, refreshed when nearly spent."""
    oidc, cache, session = runtime.web.oidc, runtime.oidc, caller.session
    now = runtime.now()
    async with runtime.sessionmaker() as db, db.begin():
        people = await sessions_store.list_session_people(db, session_id=session.id)
        rows = await tokens_store.lock_oidc_tokens(db, person_ids=people, issuer=issuer)
        mine = next((row for row in rows if row.person_id == caller.person.id), None)
        if mine is None:
            log.info("web.app_token.none_stored")
            return None
        try:
            if mine.expires_at - now > MIN_TOKEN_LEFT:
                return AppToken(
                    decrypt_token(runtime.fernet, mine.access_token_enc), mine.expires_at
                )
            if mine.refresh_token_enc is None or cache is None or oidc is None:
                log.info("web.app_token.spent", refreshable=mine.refresh_token_enc is not None)
                return None
            if oidc.issuer != issuer:
                log.warning("web.app_token.issuer_mismatch")
                return None
            spent = decrypt_token(runtime.fernet, mine.refresh_token_enc)
        except InvalidToken:
            log.warning("web.app_token.unreadable")
            return None
        try:
            tokens = await web_oidc.refresh(
                runtime.http_client, await cache.metadata(), oidc, refresh_token=spent
            )
        except (web_oidc.OidcError, httpx.HTTPError) as err:
            log.warning("web.app_token.refresh_failed", err_type=type(err).__name__)
            return None
        expires_at = now + timedelta(seconds=tokens.expires_in)
        access_enc = encrypt_token(runtime.fernet, tokens.access_token)
        refresh_enc = (
            mine.refresh_token_enc
            if tokens.refresh_token is None
            else encrypt_token(runtime.fernet, tokens.refresh_token)
        )
        # Every person of this sign-in held the old refresh token; all move to the new one.
        for row in rows:
            await tokens_store.upsert_oidc_token(
                db,
                tenant_id=row.tenant_id,
                person_id=row.person_id,
                issuer=issuer,
                access_token_enc=access_enc,
                refresh_token_enc=refresh_enc,
                expires_at=expires_at,
            )
    log.info("web.app_token.refreshed")
    return AppToken(tokens.access_token, expires_at)


async def _exchanged_token(
    runtime: WebRuntime, caller: WebCaller, app: EmbeddedAppSettings, issuer: str
) -> AppToken | None:
    """A token for the chat account the caller acts as, by token exchange.

    Kept with the person's tokens and exchanged again when nearly spent. A refusal is
    remembered for a while, so a page load does not ask the provider each time.
    """
    person, now = caller.person, runtime.now()
    refused_key = (person.id, app.key)
    if (until := runtime.exchange_refused.get(refused_key)) is not None and until > now:
        return None
    oidc, cache, key = runtime.web.oidc, runtime.oidc, runtime.identity_key
    server = next(
        (s for s in runtime.settings.mcp_identity.servers if s.name == app.identity_server), None
    )
    if oidc is None or cache is None or key is None or server is None:
        log.warning("web.app_token.exchange_unconfigured", app=app.key)
        return None
    async with runtime.sessionmaker() as db:
        stored = await tokens_store.get_oidc_token(db, person_id=person.id, issuer=issuer)
        platform, user_id = await linked_identity(db, person)
        tenant = await tenants_store.get_tenant(db, person.tenant_id)
    try:
        if stored is not None and stored.expires_at - now > MIN_TOKEN_LEFT:
            return AppToken(
                decrypt_token(runtime.fernet, stored.access_token_enc), stored.expires_at
            )
    except InvalidToken:
        log.warning("web.app_token.unreadable")
    if platform not in CHAT_PLATFORMS or tenant is None or tenant.platform != platform:
        return None
    subject = web_oidc.sign_subject_token(
        key=key,
        issuer=server.issuer,
        audience=issuer,
        subject=f"{platform}:{user_id}",
        workspace=f"{platform}:{tenant.external_id}",
        now=now,
        jti=uuid.uuid4().hex,
    )
    try:
        tokens = await web_oidc.exchange_subject_token(
            runtime.http_client, await cache.metadata(), oidc, subject_token=subject
        )
    except (web_oidc.OidcError, httpx.HTTPError) as err:
        log.warning("web.app_token.exchange_failed", app=app.key, err_type=type(err).__name__)
        _remember_refusal(runtime, refused_key, now + EXCHANGE_BACKOFF)
        return None
    life = timedelta(seconds=tokens.expires_in) if tokens.expires_in > 0 else DEFAULT_TOKEN_LIFE
    expires_at = now + life
    async with runtime.sessionmaker() as db, db.begin():
        # No refresh token: an exchanged token is exchanged again, and a sign-in's own
        # refresh token, if any, stays.
        await tokens_store.upsert_oidc_token(
            db,
            tenant_id=person.tenant_id,
            person_id=person.id,
            issuer=issuer,
            access_token_enc=encrypt_token(runtime.fernet, tokens.access_token),
            refresh_token_enc=None,
            expires_at=expires_at,
        )
    runtime.exchange_refused.pop(refused_key, None)
    log.info("web.app_token.exchanged", app=app.key, platform=platform)
    return AppToken(tokens.access_token, expires_at)


def _remember_refusal(runtime: WebRuntime, key: tuple[uuid.UUID, str], until: datetime) -> None:
    refused = runtime.exchange_refused
    if len(refused) >= _MAX_REFUSALS:
        # Oldest first: insertion order.
        del refused[next(iter(refused))]
    refused[key] = until


def _bearer(token: str | None) -> dict[str, str]:
    return {} if token is None else {"Authorization": f"Bearer {token}"}


async def app_status(
    http: httpx.AsyncClient, app: EmbeddedAppSettings, *, token: str | None
) -> AppStatus:
    """Whether the app answers its manifest, asked as the caller."""
    try:
        response = await http.get(
            f"{app.origin}/embed/manifest", headers=_bearer(token), timeout=APP_TIMEOUT_S
        )
    except httpx.HTTPError as err:
        log.info("web.app_probe.failed", app=app.key, err_type=type(err).__name__)
        return "error"
    return "connected" if response.is_success else "error"


def apps_router(runtime: WebRuntime) -> APIRouter:
    router = APIRouter(prefix="/api/apps")

    @router.post("/{key}/token")
    async def token(  # pyright: ignore[reportUnusedFunction]
        key: str, caller: Annotated[WebCaller, Depends(current_caller)]
    ) -> TokenOut:
        app = app_for(runtime.web, key)
        got = None if app is None else await app_token(runtime, caller, app)
        if got is None:
            raise HTTPException(status_code=404, detail="This app has no sign-in for you.")
        return TokenOut(access_token=got.access_token, expires_at=got.expires_at)

    return router
