"""Shared per-event AsyncWebClient builder for Slack interaction handlers.

``resolve_web_client`` factors out the token→client path that every slash-command
and block-action handler needs (STURN-03: no singleton client, no caching).

Factored from ``app.py::_handle_app_mention`` steps 2–4 so each surface
handler can call one function instead of duplicating the decrypt dance.
"""

from __future__ import annotations

from daimon.adapters.slack.runtime import SlackRuntime
from daimon.core.github_credentials import build_multifernet, decrypt_token
from daimon.core.stores.slack_bot_tokens import get_slack_bot_token
from slack_sdk.http_retry.async_handler import AsyncRetryHandler
from slack_sdk.http_retry.builtin_async_handlers import (
    AsyncRateLimitErrorRetryHandler,
    async_default_handlers,
)
from slack_sdk.web.async_client import AsyncWebClient


def build_retry_handlers() -> list[AsyncRetryHandler]:
    """Retry handlers for every AsyncWebClient we construct.

    slack_sdk's default is connection-error retries only, so a 429 surfaces
    immediately as ``SlackApiError``. Slack caps non-Marketplace apps at one
    ``conversations.history``/``conversations.replies`` call per minute, and
    the listener boundary posts nothing on failure, so an unretried 429 reads
    as a dead bot. ``AsyncRateLimitErrorRetryHandler`` honours ``Retry-After``;
    its default single retry is deliberate, since that wait can be 60s.
    """
    return [*async_default_handlers(), AsyncRateLimitErrorRetryHandler()]


async def resolve_web_client(runtime: SlackRuntime, *, team_id: str) -> AsyncWebClient | None:
    """Build a fresh per-event AsyncWebClient from the stored bot token.

    Mirrors ``_handle_app_mention`` steps 2–4 exactly (app.py:240-252).
    The client is constructed on every call and NEVER cached on ``runtime``
    or at module scope (STURN-03: token is decrypted per-event).

    Args:
        runtime: Injected ``SlackRuntime`` (settings, sessionmaker).
        team_id: Slack workspace ID from the verified Socket Mode payload.

    Returns:
        A fresh ``AsyncWebClient(token=...)`` for the workspace, or ``None``
        if no token row exists for ``team_id`` (caller logs / drops the event).
        ``InvalidToken`` or ``SQLAlchemyError`` propagate to the caller's
        listener-boundary catch — do NOT wrap them here.
    """
    async with runtime.sessionmaker() as s:
        row = await get_slack_bot_token(s, team_id=team_id)
    if row is None:
        return None
    fernet = build_multifernet(tuple(k.get_secret_value() for k in runtime.settings.crypto.keys))
    token = decrypt_token(fernet, row.encrypted_token)
    return AsyncWebClient(  # per-event only — never cache
        token=token, retry_handlers=build_retry_handlers()
    )
