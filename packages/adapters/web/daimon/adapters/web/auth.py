"""Sign-in routes: provider list, redirect flow, logout and the dev-only sign-in."""

from __future__ import annotations

import hmac
import secrets
from typing import Literal

import httpx
import structlog
from daimon.adapters.web.caller import WebCaller, current_caller, ttl_of
from daimon.adapters.web.cookies import (
    FLOW_TTL,
    SignInFlow,
    clear_cookie,
    flow_cookie_name,
    new_session_token,
    open_flow,
    seal_flow,
    session_cookie_name,
    session_token,
    set_cookie,
    token_sha256,
)
from daimon.adapters.web.providers import SignInProvider, build_providers
from daimon.adapters.web.redirects import (
    safe_next,
    signin_error_url,
    workspaces_url,
)
from daimon.adapters.web.runtime import WebRuntime
from daimon.core.errors import DaimonError, UserFacingError
from daimon.core.platform_sign_in import code_challenge_s256
from daimon.core.stores import web_sessions as sessions_store
from daimon.core.stores.domain import WebPersonRow
from daimon.core.stores.tenants import list_tenants_by_platform
from daimon.core.web_identity import VerifiedIdentity, WebSignInRefused, resolve_web_people
from daimon.core.web_login import connect_account, sign_in_login
from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

log = structlog.get_logger()

_FAILED = "Sign-in didn't work. Try again."
_EXPIRED = "Your sign-in took too long. Try again."
_CANCELLED = "Sign-in was cancelled."
_VISITOR_CONNECT = "Guests can't connect an account."


class ProviderOut(BaseModel):
    id: str
    label: str
    kind: Literal["redirect", "dev"]


class ConnectStart(BaseModel):
    next: str | None = None


class DevSignIn(BaseModel):
    provider: str
    email: str | None = None


def _redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


def auth_router(runtime: WebRuntime) -> APIRouter:
    web = runtime.web
    providers = build_providers(runtime)
    base = str(web.public_url).rstrip("/")
    router = APIRouter()

    def redirect_uri(provider_id: str) -> str:
        return f"{base}/auth/{provider_id}/callback"

    def fail(message: str) -> RedirectResponse:
        response = _redirect(signin_error_url(message))
        clear_cookie(response, web, name=flow_cookie_name(web))
        return response

    async def open_session(
        identity: VerifiedIdentity, people: list[WebPersonRow]
    ) -> tuple[str, list[WebPersonRow]]:
        """Save the sign-in on the account's login, reaching the connected accounts'
        workspaces too. Returns the cookie value that names it and everyone it reaches."""
        login_id, people = await sign_in_login(
            runtime.sessionmaker, identity=identity, people=people, now=runtime.now()
        )
        token = new_session_token()
        async with runtime.sessionmaker() as db, db.begin():
            await sessions_store.create_session(
                db,
                token_sha256=token_sha256(token),
                provider=identity.provider,
                issuer="",
                subject=identity.subject,
                people=people,
                ttl=ttl_of(runtime),
                now=runtime.now(),
                login_id=login_id,
            )
        return token, people

    def give_session(response: Response, token: str) -> None:
        set_cookie(
            response, web, name=session_cookie_name(web), value=token, max_age=ttl_of(runtime)
        )

    @router.get("/auth/providers")
    async def list_providers() -> list[ProviderOut]:  # pyright: ignore[reportUnusedFunction]
        out = [ProviderOut(id=p.id, label=p.label, kind="redirect") for p in providers.values()]
        if web.dev:
            out.append(ProviderOut(id="dev", label="Dev user", kind="dev"))
        return out

    @router.post("/auth/logout")
    async def logout(request: Request, response: Response) -> dict[str, bool]:  # pyright: ignore[reportUnusedFunction]
        token = session_token(request, web)
        if token:
            async with runtime.sessionmaker() as db, db.begin():
                await sessions_store.delete_session(db, token_sha256=token_sha256(token))
        clear_cookie(response, web, name=session_cookie_name(web))
        return {"ok": True}

    async def signed_in_caller(request: Request) -> WebCaller:
        """The person signed in on this browser, for connecting another account to them."""
        return await current_caller(request, Response())

    async def finish_connect(
        request: Request, flow: SignInFlow, identity: VerifiedIdentity
    ) -> Response:
        """Connect the proven account to the session that started the flow on this browser."""
        try:
            caller = await signed_in_caller(request)
        except HTTPException:
            return fail(_EXPIRED)
        if str(caller.session.id) != flow.connect_session:
            return fail(_EXPIRED)
        if caller.person.kind == "visitor":
            return fail(_VISITOR_CONNECT)
        try:
            result = await connect_account(
                runtime.sessionmaker,
                web_session=caller.session,
                person=caller.person,
                identity=identity,
                now=runtime.now(),
            )
        except WebSignInRefused as refused:
            log.warning("web.connect.refused", provider=identity.provider, reason=str(refused))
            return fail(str(refused))
        log.info(
            "web.account.connected",
            provider=identity.provider,
            person=str(caller.person.id),
            added=len(result.added),
            merged=result.merged,
        )
        response = _redirect(workspaces_url(flow.next) if result.added else flow.next)
        clear_cookie(response, web, name=flow_cookie_name(web))
        return response

    def new_flow(next: str | None, connect_session: str | None = None) -> SignInFlow:
        return SignInFlow(
            verifier=secrets.token_urlsafe(48),
            state=secrets.token_urlsafe(24),
            next=safe_next(next),
            connect_session=connect_session,
        )

    async def authorize(
        provider: SignInProvider, flow: SignInFlow, *, interactive: bool = False
    ) -> str:
        return await provider.authorize_url(
            state=flow.state,
            code_challenge=code_challenge_s256(flow.verifier),
            redirect_uri=redirect_uri(provider.id),
            interactive=interactive,
        )

    def hold_flow(response: Response, flow: SignInFlow) -> None:
        set_cookie(
            response,
            web,
            name=flow_cookie_name(web),
            value=seal_flow(runtime.fernet, flow),
            max_age=FLOW_TTL,
        )

    @router.get("/auth/{provider_id}/start")
    async def start(  # pyright: ignore[reportUnusedFunction]
        provider_id: str, next: str | None = None
    ) -> Response:
        provider = providers.get(provider_id)
        if provider is None:
            raise HTTPException(status_code=404, detail="Unknown sign-in method.")
        flow = new_flow(next)
        try:
            url = await authorize(provider, flow)
        except (DaimonError, httpx.HTTPError) as exc:
            log.warning("web.signin.start_failed", provider=provider_id, error=type(exc).__name__)
            return fail(_FAILED)
        response = _redirect(url)
        hold_flow(response, flow)
        return response

    @router.post("/auth/{provider_id}/connect")
    async def start_connect(  # pyright: ignore[reportUnusedFunction]
        provider_id: str, body: ConnectStart, request: Request, response: Response
    ) -> dict[str, str]:
        """Begin connecting another account to the signed-in session; answers with the
        provider's URL. A POST, so the page's own header and origin check guard it: a link
        opened by a stray GET could attach someone else's account to the session."""
        provider = providers.get(provider_id)
        if provider is None:
            raise HTTPException(status_code=404, detail="Unknown sign-in method.")
        caller = await signed_in_caller(request)
        if caller.person.kind == "visitor":
            raise HTTPException(status_code=403, detail=_VISITOR_CONNECT)
        flow = new_flow(body.next, str(caller.session.id))
        try:
            url = await authorize(provider, flow, interactive=True)
        except (DaimonError, httpx.HTTPError) as exc:
            log.warning("web.connect.start_failed", provider=provider_id, error=type(exc).__name__)
            raise HTTPException(status_code=502, detail=_FAILED) from None
        hold_flow(response, flow)
        return {"url": url}

    @router.get("/auth/{provider_id}/callback")
    async def callback(  # pyright: ignore[reportUnusedFunction]
        provider_id: str,
        request: Request,
        code: str | None = None,
        state: str | None = None,
        error: str | None = None,
    ) -> Response:
        provider = providers.get(provider_id)
        if provider is None:
            raise HTTPException(status_code=404, detail="Unknown sign-in method.")
        flow = open_flow(runtime.fernet, request.cookies.get(flow_cookie_name(web)))
        if error is not None:
            log.warning("web.signin.provider_error", provider=provider_id, error=error)
            return fail(_CANCELLED if error == "access_denied" else _FAILED)
        if (
            flow is None
            or not code
            or not state
            or not hmac.compare_digest(state.encode(), flow.state.encode())
        ):
            return fail(_EXPIRED)
        try:
            identity = await provider.complete(
                code=code, code_verifier=flow.verifier, redirect_uri=redirect_uri(provider_id)
            )
        except UserFacingError as refused:
            log.warning("web.signin.refused", provider=provider_id, reason=str(refused))
            return fail(str(refused))
        except (DaimonError, httpx.HTTPError) as exc:
            log.warning("web.signin.rejected", provider=provider_id, error=type(exc).__name__)
            return fail(_FAILED)
        if flow.connect_session is not None:
            return await finish_connect(request, flow, identity)
        try:
            people = await resolve_web_people(runtime.sessionmaker, identity=identity)
        except WebSignInRefused as refused:
            log.warning(
                "web.signin.refused",
                provider=provider_id,
                reason=str(refused),
                workspaces=[workspace for workspace, _ in identity.workspaces],
            )
            return fail(str(refused))
        token, people = await open_session(identity, people)
        response = _redirect(workspaces_url(flow.next) if len(people) > 1 else flow.next)
        give_session(response, token)
        clear_cookie(response, web, name=flow_cookie_name(web))
        return response

    if web.dev:

        @router.post("/auth/mock")
        async def dev_sign_in(body: DevSignIn, response: Response) -> dict[str, bool]:  # pyright: ignore[reportUnusedFunction]
            if body.provider != "dev":
                raise HTTPException(status_code=400, detail="Unknown sign-in method.")
            # Every ready Discord workspace vouches for the dev user; dev is loopback only.
            email = (body.email or "dev@example.com").strip().lower()
            workspaces = tuple(
                (t.external_id, t.external_id)
                for t in await list_tenants_by_platform(runtime.sessionmaker, platform="discord")
            )
            identity = VerifiedIdentity(
                provider="discord",
                subject=f"dev:{email}",
                display_name=email.split("@", 1)[0].title(),
                workspaces=workspaces,
            )
            try:
                people = await resolve_web_people(runtime.sessionmaker, identity=identity)
            except WebSignInRefused as refused:
                raise HTTPException(status_code=403, detail=str(refused)) from None
            token, _ = await open_session(identity, people)
            give_session(response, token)
            return {"ok": True}

    return router
