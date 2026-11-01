"""Sign-in methods. Each is one object the auth routes drive; adding a method adds one here.

A redirect method builds the URL the browser is sent to, then turns the code it comes
back with into a verified identity. Chat-platform sign-in reuses the deployment's own
OAuth apps: the login apps of the MCP hub for Discord and Slack, and the Connect
Microsoft app registration for Microsoft. Each needs `<public url>/auth/<id>/callback`
registered as a redirect URI.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

import httpx
from daimon.adapters.web.runtime import WebRuntime
from daimon.core.config import MicrosoftAccountsSettings, WebOidcSettings
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
from daimon.core.web_oidc import (
    OidcCache,
    OidcTokens,
    build_authorize_url,
    exchange_code,
)


@dataclass(frozen=True)
class SignedIn:
    """The outcome of a finished sign-in."""

    identity: VerifiedIdentity
    # Provider tokens, when the method has any worth keeping.
    tokens: OidcTokens | None = None


class SignInProvider(Protocol):
    id: str
    label: str
    icon: str | None

    async def authorize_url(
        self,
        *,
        state: str,
        nonce: str,
        code_challenge: str,
        redirect_uri: str,
        interactive: bool = False,
    ) -> str:
        """Where to send the browser. `interactive` asks for a visible consent screen, for
        a person linking an account rather than signing in."""
        ...

    async def complete(
        self, *, code: str, code_verifier: str, nonce: str, redirect_uri: str, now: datetime
    ) -> SignedIn: ...


class OidcSignIn:
    """Sign in through the configured OpenID Connect provider."""

    id = "oidc"

    def __init__(
        self, http: httpx.AsyncClient, settings: WebOidcSettings, cache: OidcCache
    ) -> None:
        self._http = http
        self._settings = settings
        self._cache = cache
        self.label = settings.label
        self.icon = settings.icon

    async def authorize_url(
        self,
        *,
        state: str,
        nonce: str,
        code_challenge: str,
        redirect_uri: str,
        interactive: bool = False,
    ) -> str:
        return build_authorize_url(
            await self._cache.metadata(),
            self._settings,
            state=state,
            nonce=nonce,
            code_challenge=code_challenge,
            redirect_uri=redirect_uri,
        )

    async def complete(
        self, *, code: str, code_verifier: str, nonce: str, redirect_uri: str, now: datetime
    ) -> SignedIn:
        tokens = await exchange_code(
            self._http,
            await self._cache.metadata(),
            self._settings,
            code=code,
            code_verifier=code_verifier,
            redirect_uri=redirect_uri,
        )
        return SignedIn(await self._cache.verify(tokens, nonce=nonce, now=now), tokens)


class DiscordSignIn:
    """Sign in with Discord; the person's servers say which workspaces they reach."""

    id = "discord"
    label = "Discord"
    icon: str | None = None

    def __init__(self, http: httpx.AsyncClient, client_id: str, client_secret: str) -> None:
        self._http = http
        self._client_id = client_id
        self._client_secret = client_secret

    async def authorize_url(
        self,
        *,
        state: str,
        nonce: str,
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
        self, *, code: str, code_verifier: str, nonce: str, redirect_uri: str, now: datetime
    ) -> SignedIn:
        token = await exchange_discord_code(
            self._http,
            client_id=self._client_id,
            client_secret=self._client_secret,
            code=code,
            code_verifier=code_verifier,
            redirect_uri=redirect_uri,
        )
        user = await fetch_discord_user(self._http, access_token=token)
        return SignedIn(
            VerifiedIdentity(
                provider="discord",
                issuer="",
                subject=user.user_id,
                display_name=user.name,
                email=None,
                email_verified=False,
                groups=(),
                workspaces=tuple(user.workspaces),
                platform_user_id=user.user_id,
            )
        )


class SlackSignIn:
    """Sign in with Slack; the grant names the one workspace it was given in."""

    id = "slack"
    label = "Slack"
    icon: str | None = None

    def __init__(self, http: httpx.AsyncClient, client_id: str, client_secret: str) -> None:
        self._http = http
        self._client_id = client_id
        self._client_secret = client_secret

    async def authorize_url(
        self,
        *,
        state: str,
        nonce: str,
        code_challenge: str,
        redirect_uri: str,
        interactive: bool = False,
    ) -> str:
        return slack_authorize_url(
            client_id=self._client_id, redirect_uri=redirect_uri, state=state
        )

    async def complete(
        self, *, code: str, code_verifier: str, nonce: str, redirect_uri: str, now: datetime
    ) -> SignedIn:
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
        return SignedIn(
            VerifiedIdentity(
                provider="slack",
                issuer="",
                subject=f"{grant.team_id}:{grant.user_id}",
                display_name=name or grant.user_id,
                email=None,
                email_verified=False,
                groups=(),
                workspaces=((grant.team_id, grant.team_name or grant.team_id),),
                platform_user_id=grant.user_id,
            )
        )


class MicrosoftSignIn:
    """Sign in with a Microsoft work account of the directory Teams is set up for."""

    id = "microsoft"
    label = "Microsoft"
    icon: str | None = None

    def __init__(self, http: httpx.AsyncClient, config: MicrosoftAccountsSettings) -> None:
        self._http = http
        self._config = config

    async def authorize_url(
        self,
        *,
        state: str,
        nonce: str,
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
        )

    async def complete(
        self, *, code: str, code_verifier: str, nonce: str, redirect_uri: str, now: datetime
    ) -> SignedIn:
        tokens = await exchange_microsoft_code(
            self._http,
            self._config,
            code=code,
            code_verifier=code_verifier,
            redirect_uri=redirect_uri,
            scopes=MICROSOFT_SIGN_IN_SCOPES,
        )
        signed = verify_microsoft_sign_in(tokens, self._config)
        return SignedIn(
            VerifiedIdentity(
                provider="microsoft",
                issuer="",
                subject=f"{signed.tenant_id}:{signed.object_id}",
                display_name=signed.name,
                email=None,
                email_verified=False,
                groups=(),
                workspaces=((signed.tenant_id, "Microsoft Teams"),),
                platform_user_id=signed.object_id,
            )
        )


def build_providers(runtime: WebRuntime) -> dict[str, SignInProvider]:
    """The methods this deployment offers: listed in settings and fully configured."""
    providers: dict[str, SignInProvider] = {}
    web, http, hub = runtime.web, runtime.http_client, runtime.settings.hub
    if "oidc" in web.providers and web.oidc is not None and runtime.oidc is not None:
        providers["oidc"] = OidcSignIn(http, web.oidc, runtime.oidc)
    if "discord" in web.providers and hub.discord_configured:
        assert hub.discord_client_id is not None and hub.discord_client_secret is not None
        providers["discord"] = DiscordSignIn(
            http, hub.discord_client_id, hub.discord_client_secret.get_secret_value()
        )
    if "slack" in web.providers and hub.slack_configured:
        assert hub.slack_client_id is not None and hub.slack_client_secret is not None
        providers["slack"] = SlackSignIn(
            http, hub.slack_client_id, hub.slack_client_secret.get_secret_value()
        )
    microsoft = runtime.settings.microsoft_accounts
    if "microsoft" in web.providers and microsoft.configured:
        providers["microsoft"] = MicrosoftSignIn(http, microsoft)
    return providers
