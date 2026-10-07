"""Post with an agent's Slack header, with pre-reinstall scope fallback."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, cast

from daimon.core.agent_identity import AgentIdentity
from daimon.core.slack_customize_scope import (
    _NO_CUSTOMIZE_SCOPE as _NO_CUSTOMIZE_SCOPE,  # pyright: ignore[reportPrivateUsage]
)
from daimon.core.slack_customize_scope import (
    missing_customize_scope,
    remember_missing_customize_scope,
)
from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_client import AsyncWebClient
from slack_sdk.web.async_slack_response import AsyncSlackResponse


async def post_as_agent(
    client: AsyncWebClient,
    identity: AgentIdentity | None,
    on_customized: Callable[[bool], None] | None = None,
    **kwargs: Any,  # noqa: ANN401
) -> AsyncSlackResponse:
    """Retry once as the bot only when Slack explicitly needs customize scope."""
    token = getattr(client, "token", None)
    if identity is None or identity.builtin or missing_customize_scope(token):
        response = await client.chat_postMessage(**kwargs)  # pyright: ignore[reportUnknownMemberType, reportArgumentType]
        if on_customized is not None:
            on_customized(False)
        return response
    custom = {"username": identity.name}
    if identity.avatar_url is not None:
        custom["icon_url"] = identity.avatar_url
    try:
        response = await client.chat_postMessage(**kwargs, **custom)  # pyright: ignore[reportUnknownMemberType, reportArgumentType]
        if on_customized is not None:
            on_customized(True)
        return response
    except SlackApiError as exc:
        raw_data = exc.response.data  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
        if not isinstance(raw_data, dict):
            raise
        data = cast(dict[str, object], raw_data)
        needed = {scope.strip() for scope in str(data.get("needed", "")).split(",")}
        if data.get("error") != "missing_scope" or "chat:write.customize" not in needed:
            raise
        remember_missing_customize_scope(token)
        response = await client.chat_postMessage(**kwargs)  # pyright: ignore[reportUnknownMemberType, reportArgumentType]
        if on_customized is not None:
            on_customized(False)
        return response
