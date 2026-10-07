"""Post with an agent's Slack header, with pre-reinstall scope fallback."""

from __future__ import annotations

from typing import Any, cast

from daimon.core.agent_identity import AgentIdentity
from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_client import AsyncWebClient
from slack_sdk.web.async_slack_response import AsyncSlackResponse

_NO_CUSTOMIZE_SCOPE: set[str] = set()


async def post_as_agent(
    client: AsyncWebClient,
    identity: AgentIdentity | None,
    **kwargs: Any,  # noqa: ANN401
) -> AsyncSlackResponse:
    """Retry once as the bot only when Slack explicitly needs customize scope."""
    token = client.token
    if identity is None or identity.builtin or (token and token in _NO_CUSTOMIZE_SCOPE):
        return await client.chat_postMessage(**kwargs)  # pyright: ignore[reportUnknownMemberType, reportArgumentType]
    custom = {"username": identity.name}
    if identity.avatar_url is not None:
        custom["icon_url"] = identity.avatar_url
    try:
        return await client.chat_postMessage(**kwargs, **custom)  # pyright: ignore[reportUnknownMemberType, reportArgumentType]
    except SlackApiError as exc:
        data = cast(dict[str, object], exc.response.data)  # pyright: ignore[reportUnknownMemberType]
        needed = {scope.strip() for scope in str(data.get("needed", "")).split(",")}
        if data.get("error") != "missing_scope" or "chat:write.customize" not in needed:
            raise
        if token:
            _NO_CUSTOMIZE_SCOPE.add(token)
        return await client.chat_postMessage(**kwargs)  # pyright: ignore[reportUnknownMemberType, reportArgumentType]
