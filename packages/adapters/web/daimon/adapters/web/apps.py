"""Embedded apps: the person's sign-in token for an app, the app's status, an agent's panels.

An app gets the access token of the provider it trusts, and only for a person signed
in with that provider. Refresh tokens rotate, so refreshes are serialized per sign-in.
"""

from __future__ import annotations

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
from daimon.core.stores import web_oidc_tokens as tokens_store
from daimon.core.stores import web_sessions as sessions_store
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

log = structlog.get_logger(__name__)

#: A token with less left than this is refreshed before it is handed out.
MIN_TOKEN_LEFT = timedelta(seconds=60)
#: How long the status probe waits for the app.
APP_TIMEOUT_S = 3.0


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
    """The caller's access token for `app`, refreshed when nearly spent; None when none."""
    issuer, oidc, cache = app.oidc_issuer, runtime.web.oidc, runtime.oidc
    session = caller.session
    if issuer is None or session.provider != "oidc" or session.issuer != issuer:
        return None
    now = runtime.now()
    async with runtime.sessionmaker() as db, db.begin():
        people = await sessions_store.list_session_people(db, session_id=session.id)
        rows = await tokens_store.lock_oidc_tokens(db, person_ids=people, issuer=issuer)
        mine = next((row for row in rows if row.person_id == caller.person.id), None)
        if mine is None:
            return None
        try:
            if mine.expires_at - now > MIN_TOKEN_LEFT:
                return AppToken(
                    decrypt_token(runtime.fernet, mine.access_token_enc), mine.expires_at
                )
            if mine.refresh_token_enc is None or cache is None or oidc is None:
                return None
            if oidc.issuer != issuer:
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
