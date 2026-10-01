"""`/oauth/mcp/start` and `/oauth/mcp/callback` — the browser half of an MCP OAuth grant.

The chat adapter minted the flow row on the requester's click and handed
them a start link nobody else saw. `start` opens it: discovery, dynamic
client registration, then a redirect to the authorization server.
`callback` spends the flow (the atomic consume is the replay gate),
exchanges the code, stores an `mcp_oauth` credential in the requester's
per-agent vault, attaches the server to the agent and edits the card the
request was posted as. Pages are static copy; nothing from the request or
an upstream error body is interpolated into HTML.
"""

from __future__ import annotations

import html
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Literal

import anthropic
import httpx
import structlog
from cryptography.fernet import MultiFernet
from daimon.adapters.mcp.oauth_slack import _page  # pyright: ignore[reportPrivateUsage]
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools.discord._credential_button import (
    edit_card_state as edit_discord_card_state,
)
from daimon.adapters.mcp.tools.slack._credential_button import (
    edit_card_state_for_tenant as edit_slack_card_state,
)
from daimon.adapters.mcp.tools.teams._send import edit_teams_card_state
from daimon.core.continuity.messages import ConfigurationChange
from daimon.core.errors import DaimonError
from daimon.core.mcp_oauth import complete_mcp_oauth_flow, prepare_authorization
from daimon.core.stores import credential_requests as requests_store
from daimon.core.stores import mcp_oauth_flows as flows_store
from daimon.core.stores.domain import CredentialRequestRow, McpOAuthFlowRow
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

log = structlog.get_logger(__name__)

Handler = Callable[[Request], Awaitable[Response]]
HttpClientFactory = Callable[[], httpx.AsyncClient]

_ErrorKind = Literal[
    "expired", "unconfigured", "discovery_failed", "exchange_failed", "declined", "agent_gone"
]
_ERROR_COPY: dict[_ErrorKind, tuple[str, str, int]] = {
    "expired": (
        "This sign-in link has expired",
        "Ask the agent to connect the server again and use the new link within ten minutes.",
        400,
    ),
    "unconfigured": (
        "This deployment cannot finish sign-in",
        "Ask the operator to finish the daimon setup, then try again.",
        500,
    ),
    "discovery_failed": (
        "That server does not offer sign-in daimon can drive",
        "It published no OAuth metadata or refused to register daimon as a client. "
        "If it accepts a token instead, ask the agent for a private token form.",
        502,
    ),
    "exchange_failed": (
        "Sign-in did not complete",
        "The server refused the sign-in result. Ask the agent to connect it again.",
        502,
    ),
    "declined": (
        "Sign-in was cancelled",
        "Nothing was connected. Ask the agent again whenever you want to connect it.",
        200,
    ),
    "agent_gone": (
        "Signed in, but the agent is gone",
        "Your connection is stored, but the agent it was for no longer exists, so nothing "
        "was attached. Ask again from an agent that still answers.",
        200,
    ),
}


def _error_page(kind: _ErrorKind) -> HTMLResponse:
    headline, body_text, status = _ERROR_COPY[kind]
    body = (
        f"<h1>{html.escape(headline, quote=False)}</h1><p>{html.escape(body_text, quote=False)}</p>"
    )
    return _page(
        title="daimon — connection", state_bar=" status-bar--rose", body_html=body, status=status
    )


def _success_page(*, server_name: str, agent_name: str) -> HTMLResponse:
    body = (
        f"<h1>Connected {html.escape(server_name, quote=False)}</h1>"
        f"<p>{html.escape(agent_name, quote=False)} can use your connection from your next "
        "message. You can close this tab and go back to the conversation.</p>"
    )
    return _page(title="daimon — connected", state_bar="", body_html=body)


def build_oauth_mcp_routes(
    *,
    runtime: McpRuntime,
    fernet: MultiFernet,
    http_client_factory: HttpClientFactory,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> tuple[Handler, Handler]:
    """Return `(start_handler, callback_handler)` bound to this deployment."""
    settings = runtime.settings

    async def _load_request(flow: McpOAuthFlowRow) -> CredentialRequestRow | None:
        async with runtime.session_factory() as session:
            return await requests_store.peek_credential_request(session, token=flow.request_token)

    async def start_handler(request: Request) -> Response:
        state = request.query_params.get("state", "")
        async with runtime.session_factory() as session:
            flow = await flows_store.get_flow(session, state=state) if state else None
        if flow is None or flow.used_at is not None or flow.expires_at <= now():
            return _error_page("expired")
        try:
            async with (
                http_client_factory() as http,
                runtime.session_factory() as session,
                session.begin(),
            ):
                prepared = await prepare_authorization(session, http, flow=flow, fernet=fernet)
        except (DaimonError, httpx.HTTPError, anthropic.AnthropicError) as err:
            # Boundary: the browser needs an answer and the diagnostic belongs
            # in the operator log, not the page.
            log.warning(
                "mcp_oauth.start_failed",
                mcp_server_url=flow.mcp_server_url,
                err_type=type(err).__name__,
                error=str(err)[:300],
            )
            return _error_page("discovery_failed")
        if prepared is None:
            return _error_page("expired")
        log.info("mcp_oauth.authorization_started", mcp_server_url=flow.mcp_server_url)
        return RedirectResponse(prepared.authorize_url, status_code=302)

    async def callback_handler(request: Request) -> Response:
        state = request.query_params.get("state", "")
        code = request.query_params.get("code", "")
        # Configuration is checked before the flow is spent: an unconfigured
        # deployment must not burn the person's one chance to finish.
        public_url = settings.mcp.public_url
        jwt_secret = settings.mcp.jwt_secret
        if public_url is None or jwt_secret is None:
            return _error_page("unconfigured")
        moment = now()
        async with runtime.session_factory() as session, session.begin():
            flow = (
                await flows_store.consume_flow(session, state=state, now=moment) if state else None
            )
        if flow is None:
            return _error_page("expired")
        request_row = await _load_request(flow)
        if request.query_params.get("error") or not code:
            log.info(
                "mcp_oauth.authorization_declined",
                mcp_server_url=flow.mcp_server_url,
                error=request.query_params.get("error", "")[:80],
            )
            if request_row is not None:
                await _settle(request_row, outcome="declined", state="refused")
            return _error_page("declined")
        try:
            async with http_client_factory() as http:
                completion = await complete_mcp_oauth_flow(
                    http,
                    runtime.client,
                    flow=flow,
                    code=code,
                    fernet=fernet,
                    jwt_secret=jwt_secret.get_secret_value().encode(),
                    public_url=str(public_url),
                    now=moment,
                    session_factory=runtime.session_factory,
                )
        except (DaimonError, httpx.HTTPError, anthropic.AnthropicError) as err:
            log.warning(
                "mcp_oauth.callback_failed",
                mcp_server_url=flow.mcp_server_url,
                err_type=type(err).__name__,
                error=str(err)[:300],
            )
            if request_row is not None:
                await _settle(request_row, outcome="write_failed", state="partial")
            return _error_page("exchange_failed")
        log.info(
            "mcp_oauth.connected",
            mcp_server_url=flow.mcp_server_url,
            attached=completion.ma_agent_id is not None,
        )
        if completion.ma_agent_id is None:
            if request_row is not None:
                await _settle(request_row, outcome="write_failed", state="partial")
            return _error_page("agent_gone")
        if request_row is not None:
            await _settle(request_row, outcome="applied", state="applied")
        agent_name = request_row.target_name if request_row is not None else None
        return _success_page(server_name=flow.server_name, agent_name=agent_name or "The agent")

    async def _settle(
        row: CredentialRequestRow,
        *,
        outcome: Literal["applied", "write_failed", "declined"],
        state: Literal["applied", "partial", "refused"],
    ) -> None:
        """Record the outcome and put the card in its final state. Never raises.

        The grant is already stored by the time this runs; a bookkeeping or
        card failure must not turn a finished sign-in into a 500 page.
        """
        try:
            async with runtime.session_factory() as session, session.begin():
                await requests_store.set_credential_request_outcome(
                    session, token=row.token, outcome=outcome
                )
            change: ConfigurationChange | None = None
            refusal: Literal["sign_in_declined"] | None = None
            if state == "refused":
                refusal = "sign_in_declined"
            else:
                change = ConfigurationChange(
                    target_name=row.target_name or "the agent",
                    kind="mcp",
                    availability="next_message" if state == "applied" else "preparation_failed",
                    detail=row.target,
                )
            if row.platform == "slack":
                await edit_slack_card_state(
                    runtime, row=row, state=state, outcome=change, refusal=refusal
                )
            elif row.platform == "teams":
                await edit_teams_card_state(
                    runtime, row=row, state=state, outcome=change, refusal=refusal
                )
            else:
                await edit_discord_card_state(
                    runtime, row=row, state=state, outcome=change, refusal=refusal
                )
        except Exception as err:
            log.warning(
                "mcp_oauth.settle_failed",
                err_type=type(err).__name__,
                outcome=outcome,
                error=str(err)[:200],
            )

    return start_handler, callback_handler
