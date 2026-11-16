"""Discord REST helpers for the scheduler: send one plain DM, read guild role names.

Best effort: every failure returns a falsy result and never raises. Inject an
httpx transport for offline tests.
"""

from __future__ import annotations

from typing import Any, cast

import httpx
import structlog

log = structlog.get_logger()

_BASE = "https://discord.com/api/v10/"


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

    async def send_dm(self, user_id: str, text: str) -> bool:
        """Send `text` to `user_id` as a direct message; whether Discord accepted it."""
        if not user_id.isascii() or not user_id.isdecimal():
            return False
        try:
            async with self._client() as client:
                opened = await client.post("users/@me/channels", json={"recipient_id": user_id})
                opened.raise_for_status()
                body = opened.json()
                channel = cast("dict[str, Any]", body).get("id") if isinstance(body, dict) else None
                if not isinstance(channel, str) or not channel.isdecimal():
                    return False
                sent = await client.post(
                    f"channels/{channel}/messages",
                    json={"content": text[:1900], "allowed_mentions": {"parse": []}},
                )
                sent.raise_for_status()
        except Exception as exc:  # best effort: any malformed reply must not break a fire
            log.info("discord_dm.send_failed", error=type(exc).__name__)
            return False
        return True

    async def role_names(self, guild_id: str) -> dict[str, str]:
        """Role id to name for a guild; empty when it cannot be read."""
        if not guild_id.isascii() or not guild_id.isdecimal():
            return {}
        try:
            async with self._client() as client:
                response = await client.get(f"guilds/{guild_id}/roles")
                response.raise_for_status()
                roles = response.json()
        except Exception as exc:  # best effort: any malformed reply must not break a fire
            log.info("discord_dm.role_names_failed", error=type(exc).__name__)
            return {}
        if not isinstance(roles, list):
            return {}
        roles = cast("list[Any]", roles)
        names: dict[str, str] = {}
        for role in roles:
            if not isinstance(role, dict):
                continue
            fields = cast("dict[str, Any]", role)
            role_id, name = fields.get("id"), fields.get("name")
            if isinstance(role_id, str) and isinstance(name, str):
                names[role_id] = name
        return names
