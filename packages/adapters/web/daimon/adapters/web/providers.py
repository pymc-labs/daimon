"""Sign-in methods. Each is one object the auth routes drive; adding a method adds one here.

A redirect method builds the URL the browser is sent to, then turns the code it comes
back with into a verified identity. Discord sign-in reuses the MCP hub's Discord login
app, which needs `<public url>/auth/discord/callback` registered as a redirect URI.
"""

from __future__ import annotations

from typing import Protocol

import httpx
from daimon.adapters.web.runtime import WebRuntime
from daimon.core.platform_sign_in import (
    discord_authorize_url,
    exchange_discord_code,
    fetch_discord_user,
)
from daimon.core.web_identity import VerifiedIdentity


class SignInProvider(Protocol):
    id: str
    label: str

    async def authorize_url(
        self, *, state: str, code_challenge: str, redirect_uri: str, interactive: bool = False
    ) -> str:
        """Where to send the browser. `interactive` asks for a visible consent screen, for
        a person linking an account rather than signing in."""
        ...

    async def complete(
        self, *, code: str, code_verifier: str, redirect_uri: str
    ) -> VerifiedIdentity: ...


class DiscordSignIn:
    """Sign in with Discord; the person's servers say which workspaces they reach."""

    id = "discord"
    label = "Discord"

    def __init__(self, http: httpx.AsyncClient, client_id: str, client_secret: str) -> None:
        self._http = http
        self._client_id = client_id
        self._client_secret = client_secret

    async def authorize_url(
        self,
        *,
        state: str,
        code_challenge: str,
        redirect_uri: str,
        interactive: bool = False,
    ) -> str:
        return discord_authorize_url(
            client_id=self._client_id,
            redirect_uri=redirect_uri,
            state=state,
            code_challenge=code_challenge,
            interactive=interactive,
        )

    async def complete(
        self, *, code: str, code_verifier: str, redirect_uri: str
    ) -> VerifiedIdentity:
        token = await exchange_discord_code(
            self._http,
            client_id=self._client_id,
            client_secret=self._client_secret,
            code=code,
            code_verifier=code_verifier,
            redirect_uri=redirect_uri,
        )
        user = await fetch_discord_user(self._http, access_token=token)
        return VerifiedIdentity(
            provider="discord",
            subject=user.user_id,
            display_name=user.name,
            workspaces=tuple(user.workspaces),
        )


def build_providers(runtime: WebRuntime) -> dict[str, SignInProvider]:
    """The methods this deployment offers: Discord, once the hub's Discord app is set."""
    hub = runtime.settings.hub
    if not hub.discord_configured:
        return {}
    assert hub.discord_client_id is not None and hub.discord_client_secret is not None
    return {
        "discord": DiscordSignIn(
            runtime.http_client,
            hub.discord_client_id,
            hub.discord_client_secret.get_secret_value(),
        )
    }
