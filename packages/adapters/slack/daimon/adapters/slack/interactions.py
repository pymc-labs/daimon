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
from slack_sdk.web.async_client import AsyncWebClient


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
    return AsyncWebClient(token=token)  # per-event only — never cache
