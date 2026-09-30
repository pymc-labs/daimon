"""Recognize Anthropic's non-transient spend limit responses."""

from __future__ import annotations

from typing import Literal, cast

import anthropic
import httpx

SpendLimit = Literal["org_cap", "user_limit"]


def spend_limit_response(response: httpx.Response) -> SpendLimit | None:
    """Classify a buffered Anthropic response, ignoring ordinary rate limits."""
    if response.status_code not in (400, 429):
        return None
    try:
        payload: object = response.json()
    except (ValueError, httpx.ResponseNotRead):
        return None
    if not isinstance(payload, dict):
        return None
    error: object = cast("dict[str, object]", payload).get("error")
    if not isinstance(error, dict):
        return None
    fields = cast("dict[str, object]", error)
    if response.status_code == 429:
        details = fields.get("details")
        if (
            fields.get("type") == "rate_limit_error"
            and isinstance(details, dict)
            and cast("dict[str, object]", details).get("error_code")
            == "enforced_spend_limit_reached"
        ):
            return "org_cap"
    elif fields.get("type") == "invalid_request_error":
        message = fields.get("message")
        if isinstance(message, str) and message.startswith(
            (
                "You have reached your specified API usage limits",
                "You have reached your specified workspace API usage limits",
            )
        ):
            return "user_limit"
    return None


def spend_limit_error(exc: object) -> SpendLimit | None:
    """Classify an SDK error, including one preserved as a TurnError cause."""
    from daimon.core.errors import TurnError

    if isinstance(exc, TurnError):
        exc = exc.cause
    if isinstance(exc, anthropic.APIStatusError):
        return spend_limit_response(exc.response)
    return None
