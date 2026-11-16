"""Discord REST helpers for the scheduler: send one plain DM, read guild role names.

Every failure raises `DiscordRestError`, whose text never holds the token or the
message. Inject an httpx transport for offline tests.
"""

from __future__ import annotations

from typing import Any, cast

import httpx
from daimon.core.errors import DaimonError

_BASE = "https://discord.com/api/v10/"


class DiscordRestError(DaimonError):
    """Discord refused the call, could not be reached or replied in an unexpected shape."""


class DiscordRest:
    def __init__(self, bot_token: str, *, transport: httpx.AsyncBaseTransport | None = None):
        self._bot_token = bot_token
        self._transport = transport

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=_BASE,
            headers={"Authorization": f"Bot {self._bot_token}"},
            timeout=5,
            transport=self._transport,
        )

    async def _call(self, method: str, path: str, body: object | None = None) -> object:
        try:
            async with self._client() as client:
                response = await client.request(method, path, json=body)
                response.raise_for_status()
                return response.json()
        except httpx.HTTPStatusError as exc:
            raise DiscordRestError(f"{method} {path}: HTTP {exc.response.status_code}") from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise DiscordRestError(f"{method} {path}: {type(exc).__name__}") from exc

    async def send_dm(self, user_id: str, text: str) -> None:
        """Send `text` to `user_id` as a direct message."""
        if not user_id.isascii() or not user_id.isdecimal():
            raise DiscordRestError("a Discord user id is a decimal number")
        body = await self._call("POST", "users/@me/channels", {"recipient_id": user_id})
        channel = cast("dict[str, Any]", body).get("id") if isinstance(body, dict) else None
        if not isinstance(channel, str) or not channel.isdecimal():
            raise DiscordRestError("users/@me/channels: no channel id in the reply")
        await self._call(
            "POST",
            f"channels/{channel}/messages",
            {"content": text[:1900], "allowed_mentions": {"parse": []}},
        )

    async def role_names(self, guild_id: str) -> dict[str, str]:
        """Role id to name for a guild; malformed entries are skipped."""
        if not guild_id.isascii() or not guild_id.isdecimal():
            raise DiscordRestError("a Discord guild id is a decimal number")
        roles = await self._call("GET", f"guilds/{guild_id}/roles")
        if not isinstance(roles, list):
            raise DiscordRestError("guilds/roles: the reply is not a list")
        names: dict[str, str] = {}
        for role in cast("list[Any]", roles):
            if not isinstance(role, dict):
                continue
            fields = cast("dict[str, Any]", role)
            role_id, name = fields.get("id"), fields.get("name")
            if isinstance(role_id, str) and isinstance(name, str):
                names[role_id] = name
        return names
