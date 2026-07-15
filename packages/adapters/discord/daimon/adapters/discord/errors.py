"""Structured error rendering for Discord adapter responses.

Maps known exception types to user-friendly markdown with emoji prefix,
bold labels, and ULID request ID suffix for cross-referencing with logs.
"""

from __future__ import annotations

import anthropic
from daimon.core.errors import (
    DaimonError,
    SpecError,
    StoreError,
)
from ulid import ULID

import discord


def generate_request_id() -> str:
    """Generate a ULID for request tracing."""
    return str(ULID())


def render_error(exc: Exception, *, request_id: str) -> str:
    """Map known exceptions to structured markdown with emoji, label, and rid."""
    if isinstance(exc, SpecError):
        return f"⚠️ **Spec validation failed**: {exc}\n`rid: {request_id}`"
    if isinstance(exc, StoreError):
        return f"⚠️ **Store error**: {exc}\n`rid: {request_id}`"
    if isinstance(exc, DaimonError):
        return f"⚠️ **Error**: {exc}\n`rid: {request_id}`"
    if isinstance(exc, anthropic.APIStatusError):
        return f"❌ **API Error ({exc.status_code})**: {exc.message}\n`rid: {request_id}`"
    if isinstance(exc, anthropic.APIConnectionError):
        return (
            f"\U0001f50c **Connection Error**: "
            f"Could not connect to Anthropic API. Please try again.\n"
            f"`rid: {request_id}`"
        )
    if isinstance(exc, anthropic.APIError):
        return f"❌ **API Error**: {exc.message}\n`rid: {request_id}`"
    if isinstance(exc, discord.HTTPException):
        return f"❌ **Discord Error ({exc.status})**: {exc.text}\n`rid: {request_id}`"
    if isinstance(exc, ValueError):
        return f"⚠️ **Invalid input**: {exc}\n`rid: {request_id}`"
    return f"❌ **Unexpected error**: {exc}\n`rid: {request_id}`"
