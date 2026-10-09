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
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools.discord._credential_button import (
    edit_card_state as edit_discord_card_state,
)
from daimon.adapters.mcp.tools.slack._credential_button import (
    edit_card_state_for_tenant as edit_slack_card_state,
)
from daimon.adapters.mcp.tools.teams._send import edit_teams_card_state
from daimon.adapters.mcp.web_icons import icon
from daimon.adapters.mcp.web_shell import render_page
from daimon.core.agent_pins import request_pin_refusal
from daimon.core.authz import (
    Action,
    AgentRef,
    Place,
    Surface,
    authorize,
    build_agent_ref,
    build_subject,
)
from daimon.core.continuity.messages import ConfigurationChange
from daimon.core.defaults.ma_index import find_agent_by_derived_uuid
from daimon.core.errors import DaimonError
from daimon.core.mcp_oauth import complete_mcp_oauth_flow, prepare_authorization
from daimon.core.mcp_oauth.complete import McpOAuthWriteRefusedError
from daimon.core.stores import credential_requests as requests_store
from daimon.core.stores import mcp_oauth_flows as flows_store
from daimon.core.stores.access_policy import AccessPolicyUnreadable, load_access_policy
from daimon.core.stores.accounts import get_account
from daimon.core.stores.domain import CredentialRequestRow, McpOAuthFlowRow, Role
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

log = structlog.get_logger(__name__)

Handler = Callable[[Request], Awaitable[Response]]
HttpClientFactory = Callable[[], httpx.AsyncClient]

_ErrorKind = Literal[
    "pinned",
    "expired",
    "unconfigured",
    "discovery_failed",
    "exchange_failed",
    "declined",
    "agent_gone",
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
    "pinned": (
        "Not connected: this agent runs only in certain channels",
        "This agent's rule runs it only in certain channels, so connections to it can only "
        "be added from a conversation inside them, or by an admin. Nothing was connected.",
        403,
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
        f'<div class="gh-status-icon">{icon("triangle-alert")}</div>'
        f"<h1>{html.escape(headline, quote=False)}</h1><p>{html.escape(body_text, quote=False)}</p>"
    )
    return render_page(
        title=headline, context="Connection", error=True, body_html=body, status=status
    )


def _success_page(
    *, server_name: str, agent_name: str, platform: str | None = None
) -> HTMLResponse:
    destinations = {
        "discord": ("Discord", "https://discord.com/app"),
        "slack": ("Slack", "https://app.slack.com/client/"),
        "teams": ("Teams", "https://teams.microsoft.com/"),
    }
    destination = destinations.get(platform or "")
    back = (
        '<div class="gh-actions"><a class="gh-primary" '
        f'href="{destination[1]}">{icon("chevron-left")}Back to {destination[0]}</a></div>'
        if destination
        else "<p>Return to chat.</p>"
    )
    body = (
        f'<div class="gh-status-icon">{icon("circle-check")}</div>'
        f"<h1>Connected {html.escape(server_name, quote=False)}</h1>"
        f"<p>{html.escape(agent_name, quote=False)} can use your connection from your next "
        f"message.</p>{back}"
    )
    return render_page(title=f"Connected {server_name}", context="Connection", body_html=body)


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
        # A flow started before a pin (or a rename) must not finish after it:
        # re-check the agent as it is now before any grant, stamp or attach.
        if await _pinned_refusal(flow, request_row):
            log.info("mcp_oauth.refused_pinned", mcp_server_url=flow.mcp_server_url)
            if request_row is not None:
                await _settle(request_row, outcome="declined", state="refused")
            return _error_page("pinned")
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
                    default=runtime.deployment_default,
                    may_write=lambda: _may_write(flow, request_row),
                    group_members=(
                        runtime.group_lookups.members if runtime.group_lookups else None
                    ),
                )
        except McpOAuthWriteRefusedError:
            # A pin landed during the code exchange: nothing was stored.
            log.info("mcp_oauth.refused_pinned_after_exchange", mcp_server_url=flow.mcp_server_url)
            if request_row is not None:
                await _settle(request_row, outcome="declined", state="refused")
            return _error_page("pinned")
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
        return _success_page(
            server_name=flow.server_name,
            agent_name=agent_name or "The agent",
            platform=request_row.platform if request_row is not None else None,
        )

    async def _pinned_refusal(
        flow: McpOAuthFlowRow, request_row: CredentialRequestRow | None
    ) -> bool:
        """Whether the pinned-agent write rule refuses finishing this flow now."""
        try:
            agent = await find_agent_by_derived_uuid(
                runtime.client, tenant_id=flow.tenant_id, agent_id=flow.agent_id
            )
        except anthropic.APIError:
            agent = None
        async with runtime.session_factory() as session:
            if request_row is not None:
                return await request_pin_refusal(session, row=request_row, agent=agent) is not None
            # No originating request to place it: it has no conversation, so
            # under any pin only an admin may finish it.
            try:
                policy = await load_access_policy(session, tenant_id=flow.tenant_id)
            except AccessPolicyUnreadable:
                return True
            account = await get_account(session, flow.account_id)
            return not authorize(
                policy,
                subject=build_subject(
                    is_admin=account is not None and account.role is Role.ADMIN,
                    platform_user_id=None,
                ),
                action=Action.CONFIGURE,
                surface=Surface.CONFIG,
                agent=(
                    AgentRef.unresolved()
                    if agent is None
                    else build_agent_ref(agent.name, agent.metadata)
                ),
                place=Place(),
            )

    async def _may_write(flow: McpOAuthFlowRow, request_row: CredentialRequestRow | None) -> bool:
        """The pinned-agent write rule asked again just before the grant is stored."""
        return not await _pinned_refusal(flow, request_row)

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
