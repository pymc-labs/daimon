"""Discord login for the /discord/mcp hub mount.

Built on FastMCP's ``DiscordProvider``. The one addition that matters is
``_extract_upstream_claims``: the proxy calls it once per code exchange (and
again on refresh) with Discord's raw token response, and embeds the return
value in the token it issues. That is where the user's guilds are fetched,
intersected with installed tenants, and turned into the tenant map every hub
tool reads. Fetching here rather than per request means one Discord call per
login instead of one per tool call.

The consent cookie name carries a platform suffix because the Slack proxy
shares this origin and both would otherwise write the same ``__Host-`` cookie
at ``path=/``, invalidating each other's in-flight authorizations.
"""

from __future__ import annotations

from typing import Any

import httpx
from daimon.adapters.mcp.hub.claims import encode_hub_claims
from daimon.core.hub_identity import resolve_hub_tenants
from fastmcp.server.auth.providers.discord import DiscordProvider
from key_value.aio.protocols import AsyncKeyValue
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

DISCORD_API = "https://discord.com/api/v10"
_SCOPES = ["identify", "guilds"]


async def fetch_discord_workspaces(
    http: httpx.AsyncClient, *, access_token: str
) -> tuple[str, list[tuple[str, str]]]:
    """Return ``(user_id, [(guild_id, guild_name), ...])`` for the token's user."""
    headers = {"Authorization": f"Bearer {access_token}"}
    me = await http.get(f"{DISCORD_API}/users/@me", headers=headers)
    me.raise_for_status()
    guilds = await http.get(f"{DISCORD_API}/users/@me/guilds", headers=headers)
    guilds.raise_for_status()
    user_id = str(me.json()["id"])
    return user_id, [(str(g["id"]), str(g["name"])) for g in guilds.json()]


class DaimonDiscordProvider(DiscordProvider):
    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        base_url: str,
        session_factory: async_sessionmaker[AsyncSession],
        client_storage: AsyncKeyValue,
        jwt_signing_key: bytes,
        allowed_client_redirect_uris: list[str],
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        super().__init__(
            client_id=client_id,
            client_secret=client_secret,
            base_url=base_url,
            required_scopes=_SCOPES,
            client_storage=client_storage,
            jwt_signing_key=jwt_signing_key,
            allowed_client_redirect_uris=allowed_client_redirect_uris,
            http_client=http_client,
        )
        self._session_factory = session_factory
        self._http = http_client or httpx.AsyncClient(timeout=10.0)

    async def _extract_upstream_claims(self, idp_tokens: dict[str, Any]) -> dict[str, Any] | None:
        user_id, guilds = await fetch_discord_workspaces(
            self._http, access_token=str(idp_tokens["access_token"])
        )
        tenants = await resolve_hub_tenants(
            self._session_factory, platform="discord", platform_user_id=user_id, workspaces=guilds
        )
        return encode_hub_claims(platform="discord", platform_user_id=user_id, tenants=tenants)

    def _cookie_name(self, base_name: str) -> str:
        return super()._cookie_name(f"{base_name}_DISCORD")
