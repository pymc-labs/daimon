"""Sign-in methods. Each is one object the auth routes drive; adding a method adds one here.

A redirect method builds the URL the browser is sent to, then turns the code it comes
back with into a verified identity. Chat-platform sign-in reuses the deployment's own
OAuth apps: the login apps of the MCP hub for Discord and Slack, and the Connect
Microsoft app registration for Microsoft. Each needs `<public url>/auth/<id>/callback`
registered as a redirect URI.
"""

from __future__ import annotations

from typing import Protocol

import httpx
from daimon.adapters.web.runtime import WebRuntime
from daimon.core.config import MicrosoftAccountsSettings
from daimon.core.microsoft_identity import build_authorize_url as microsoft_authorize_url
from daimon.core.microsoft_identity import exchange_code as exchange_microsoft_code
from daimon.core.platform_sign_in import (
    MICROSOFT_SIGN_IN_SCOPES,
    PlatformSignInError,
    discord_authorize_url,
    exchange_discord_code,
    exchange_slack_user_code,
    fetch_discord_user,
    fetch_slack_user_name,
    slack_authorize_url,
    verify_microsoft_sign_in,
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


class SlackSignIn:
    """Sign in with Slack; the grant names the one workspace it was given in."""

    id = "slack"
    label = "Slack"

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
        # No PKCE: Slack's switches the whole app to a public client for good (no client
        # secret, refresh tokens capped at 30 days). `state` and the flow cookie bind the flow.
        # Slack has no prompt to pick an account: it always shows its consent.
        return slack_authorize_url(
            client_id=self._client_id, redirect_uri=redirect_uri, state=state
        )

    async def complete(
        self, *, code: str, code_verifier: str, redirect_uri: str
    ) -> VerifiedIdentity:
        grant = await exchange_slack_user_code(
            self._http,
            client_id=self._client_id,
            client_secret=self._client_secret,
            code=code,
            redirect_uri=redirect_uri,
        )
        if grant.team_id is None or grant.user_id is None:
            raise PlatformSignInError("slack grant named no workspace or user")
        name = await fetch_slack_user_name(
            self._http, access_token=grant.access_token, user_id=grant.user_id
        )
        return VerifiedIdentity(
            provider="slack",
            subject=f"{grant.team_id}:{grant.user_id}",
            display_name=name or grant.user_id,
            workspaces=((grant.team_id, grant.team_name or grant.team_id),),
            platform_user_id=grant.user_id,
        )


class MicrosoftSignIn:
    """Sign in with a Microsoft work account of the directory Teams is set up for."""

    id = "microsoft"
    label = "Microsoft"

    def __init__(self, http: httpx.AsyncClient, config: MicrosoftAccountsSettings) -> None:
        self._http = http
        self._config = config

    async def authorize_url(
        self,
        *,
        state: str,
        code_challenge: str,
        redirect_uri: str,
        interactive: bool = False,
    ) -> str:
        return microsoft_authorize_url(
            self._config,
            redirect_uri=redirect_uri,
            state=state,
            code_challenge=code_challenge,
            scopes=MICROSOFT_SIGN_IN_SCOPES,
            select_account=interactive,
        )

    async def complete(
        self, *, code: str, code_verifier: str, redirect_uri: str
    ) -> VerifiedIdentity:
        tokens = await exchange_microsoft_code(
            self._http,
            self._config,
            code=code,
            code_verifier=code_verifier,
            redirect_uri=redirect_uri,
            scopes=MICROSOFT_SIGN_IN_SCOPES,
        )
        signed = verify_microsoft_sign_in(tokens, self._config)
        return VerifiedIdentity(
            provider="microsoft",
            subject=f"{signed.tenant_id}:{signed.object_id}",
            display_name=signed.name,
            workspaces=((signed.tenant_id, "Microsoft Teams"),),
            platform_user_id=signed.object_id,
        )


def build_providers(runtime: WebRuntime) -> dict[str, SignInProvider]:
    """The methods this deployment offers: each one whose OAuth app is set."""
    providers: dict[str, SignInProvider] = {}
    http, hub = runtime.http_client, runtime.settings.hub
    if hub.discord_configured:
        assert hub.discord_client_id is not None and hub.discord_client_secret is not None
        providers["discord"] = DiscordSignIn(
            http, hub.discord_client_id, hub.discord_client_secret.get_secret_value()
        )
    if hub.slack_configured:
        assert hub.slack_client_id is not None and hub.slack_client_secret is not None
        providers["slack"] = SlackSignIn(
            http, hub.slack_client_id, hub.slack_client_secret.get_secret_value()
        )
    microsoft = runtime.settings.microsoft_accounts
    if microsoft.configured:
        providers["microsoft"] = MicrosoftSignIn(http, microsoft)
    return providers
