"""`/api/github/connect/*`: the GitHub repo connect flow as JSON, for the web app's pages.

Open without web sign-in: the invitation, the flow cookies and the person's GitHub token
are the authority, as on the MCP host's pages. Every answer is 200 with the step's
`ConnectOutcome`; its cookies go out only as `Set-Cookie`, never in the body.
"""

from __future__ import annotations

from daimon.adapters.web.runtime import WebRuntime
from daimon.core.github_connect_admin import teams_admin_owner_ids
from daimon.core.github_connect_flow import GitHubConnectFlow
from daimon.core.github_connect_views import (
    FLOW_COOKIE,
    MAX_CONFIRM_BODY_BYTES,
    STATE_COOKIE,
    ConnectErrorView,
    ConnectOutcome,
    ConnectPage,
    read_capped,
    selection_from_json,
    selection_too_large,
)
from daimon.core.github_panel import connect_root
from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel


class TokenBody(BaseModel):
    token: str = ""


class StateBody(BaseModel):
    state: str = ""


class CallbackBody(StateBody):
    # Empty when the person cancelled on GitHub.
    code: str = ""


def respond(outcome: ConnectOutcome) -> Response:
    """The outcome as JSON, with its cookies set as the MCP pages set them."""
    response = JSONResponse(outcome.model_dump(mode="json", exclude={"cookies"}))
    for cookie in outcome.cookies:
        response.set_cookie(
            cookie.name,
            cookie.value,
            max_age=cookie.max_age,
            httponly=True,
            secure=cookie.secure,
            samesite="lax",
        )
    return response


def github_connect_router(runtime: WebRuntime) -> APIRouter:
    """The connect steps; unconfigured, each answers that GitHub setup is unavailable."""
    router = APIRouter(prefix="/api/github/connect")
    root = connect_root(runtime.settings)
    if root is None:
        unavailable = ConnectErrorView(
            message="GitHub setup is unavailable here.", status=503, action="Ask an admin."
        )

        @router.api_route("/{step}", methods=["GET", "POST"])
        async def not_configured(step: str) -> Response:  # pyright: ignore[reportUnusedFunction]
            return respond(ConnectPage(view=unavailable))

        return router
    flow = GitHubConnectFlow(
        settings=runtime.settings,
        sessionmaker=runtime.sessionmaker,
        fernet=runtime.fernet,
        deployment_default=runtime.deployment_default,
        anthropic=runtime.anthropic,
        teams_owner_ids=teams_admin_owner_ids,
    )

    def cookie(request: Request) -> str:
        return request.cookies.get(FLOW_COOKIE, "")

    @router.post("/start")
    async def start(body: TokenBody) -> Response:  # pyright: ignore[reportUnusedFunction]
        return respond(await flow.start(root, body.token))

    @router.post("/callback")
    async def callback(request: Request, body: CallbackBody) -> Response:  # pyright: ignore[reportUnusedFunction]
        return respond(
            await flow.callback(root, state=body.state, code=body.code, flow_cookie=cookie(request))
        )

    @router.post("/setup")
    async def setup(request: Request, body: StateBody) -> Response:  # pyright: ignore[reportUnusedFunction]
        state_cookie = request.cookies.get(STATE_COOKIE, "")
        return respond(
            await flow.setup(
                root, state=body.state, flow_cookie=cookie(request), state_cookie=state_cookie
            )
        )

    @router.post("/cancel")
    async def cancel(request: Request, body: StateBody) -> Response:  # pyright: ignore[reportUnusedFunction]
        return respond(await flow.cancel(root, state=body.state, flow_cookie=cookie(request)))

    @router.get("/view")
    async def view(request: Request, state: str = "") -> Response:  # pyright: ignore[reportUnusedFunction]
        return respond(await flow.load(root, state=state, flow_cookie=cookie(request)))

    @router.post("/save")
    async def save(request: Request) -> Response:  # pyright: ignore[reportUnusedFunction]
        body = await read_capped(
            request.stream(), request.headers.get("content-length"), MAX_CONFIRM_BODY_BYTES
        )
        selection = selection_too_large() if body is None else selection_from_json(body)
        if isinstance(selection, ConnectErrorView):
            return respond(ConnectPage(view=selection))
        return respond(await flow.save(root, flow_cookie=cookie(request), selection=selection))

    return router
