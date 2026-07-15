"""Slack identity guards + per-call bot-token AsyncWebClient.

Token resolution mirrors ``slack_file_proxy.py``: look up the workspace row
by team id, decrypt with the runtime MultiFernet. The bot token authenticates
the API call; authorization is evaluated per-user in ``_visibility.py``.
"""

from __future__ import annotations

from dataclasses import dataclass

from cryptography.fernet import InvalidToken
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.core.github_credentials import decrypt_token
from daimon.core.slack_oauth import build_slack_connect_url
from daimon.core.stores.slack_bot_tokens import get_slack_bot_token
from daimon.core.stores.slack_user_tokens import get_slack_user_token
from fastmcp.exceptions import ToolError
from slack_sdk.web.async_client import AsyncWebClient


def _require_slack_identity(auth: AuthIdentity) -> str:  # pyright: ignore[reportUnusedFunction]  # consumed by tools/slack/_read.py (Task 4)
    if auth.platform_user_id is None:
        raise ToolError("slack tools require a slack-bound identity")
    return auth.platform_user_id


def _require_team_id(auth: AuthIdentity) -> str:  # pyright: ignore[reportUnusedFunction]  # consumed by tools/slack/_read.py (Task 4)
    if auth.external_id is None:
        raise ToolError("slack tools require a workspace context")
    return auth.external_id


async def slack_web_client(runtime: McpRuntime, *, team_id: str) -> AsyncWebClient:
    """Resolve + decrypt the workspace bot token and build a per-call client.

    ``AsyncWebClient`` opens an aiohttp session per request when none is
    injected, so no close/context-manager is needed.
    """
    if runtime.fernet is None:
        raise ToolError("slack tools require DAIMON_CRYPTO__KEYS")
    async with runtime.session_factory() as session:
        row = await get_slack_bot_token(session, team_id=team_id)
    if row is None:
        raise ToolError("no slack installation found for this workspace")
    try:
        token = decrypt_token(runtime.fernet, row.encrypted_token)
    except InvalidToken as err:
        raise ToolError("workspace bot token could not be decrypted") from err
    return AsyncWebClient(token=token)


@dataclass(frozen=True)
class SlackReadClient:
    """A per-call read client plus which trust path it authenticates."""

    client: AsyncWebClient
    runs_as_user: bool


async def slack_read_client(
    runtime: McpRuntime, *, team_id: str, slack_user_id: str
) -> SlackReadClient:
    """Prefer the caller's xoxp token; fall back to the workspace bot token.

    A stored-but-undecryptable user token raises (the user must reconnect) —
    silently downgrading to the bot path would misreport their reach.

    ``slack_user_tokens.expires_at`` / ``encrypted_refresh_token`` are dormant
    columns: token rotation is off and there is no refresh flow by design, so
    a revoked/expired token here surfaces as a raw Slack API error
    (``token_revoked`` / ``token_expired`` / ``invalid_auth``) on the next
    call rather than a proactive refresh — see ``map_slack_api_error``.
    """
    if runtime.fernet is None:
        raise ToolError("slack tools require DAIMON_CRYPTO__KEYS")
    async with runtime.session_factory() as session:
        user_row = await get_slack_user_token(session, team_id=team_id, slack_user_id=slack_user_id)
    if user_row is not None:
        try:
            user_token = decrypt_token(runtime.fernet, user_row.encrypted_token)
        except InvalidToken as err:
            raise ToolError(
                "your connected Slack token could not be decrypted — "
                "disconnect and reconnect via /privacy"
            ) from err
        return SlackReadClient(client=AsyncWebClient(token=user_token), runs_as_user=True)
    return SlackReadClient(
        client=await slack_web_client(runtime, team_id=team_id), runs_as_user=False
    )


def build_connect_hint(
    runtime: McpRuntime, *, team_id: str, slack_user_id: str, now: float
) -> str | None:
    """Signed connect-link suffix for deny messages; None when unmintable."""
    slack_settings = runtime.settings.slack
    app_root_url = runtime.settings.mcp.app_root_url
    if slack_settings is None or app_root_url is None:
        return None
    url = build_slack_connect_url(
        app_root_url=app_root_url,
        signing_secret=slack_settings.signing_secret.get_secret_value(),
        team_id=team_id,
        slack_user_id=slack_user_id,
        now=now,
    )
    return (
        "\n\nTip for the user: connect your Slack account and daimon can read "
        f"anything you can see (and search): {url} — the link is personal and "
        "expires in about an hour; workspaces that require admin approval for "
        "app permissions may need an admin to approve first."
    )
