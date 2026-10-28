"""Google token provider — authorized-user refresh or service-account impersonation.

google-auth's ``Credentials.refresh()`` is synchronous (urllib under the
hood, ~0.4s round trip to oauth2.googleapis.com). We wrap it in
``asyncio.to_thread`` so concurrent MCP calls don't queue behind one mint.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any, ClassVar, cast

from daimon.core.broker.errors import NoBindingError, ProviderConfigError
from daimon.core.config import Settings
from daimon.core.stores.agent_google_binding import get_agent_google_binding
from google.auth.transport.requests import Request
from google.oauth2 import credentials, service_account
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


class GcloudTokenProvider:
    """Mint Google tokens from operator-configured credentials.

    Only an agent whose operator binding names ``google_user_email`` may mint
    from ``google_user_json``, and only for the binding's scopes. Any other
    binding never touches that credential. Other emails use the service account
    with domain-wide delegation on the bound scopes.
    """

    service: ClassVar[str] = "gcloud"

    async def mint_token(
        self,
        *,
        account_id: uuid.UUID,
        agent_id: uuid.UUID | None,
        sessionmaker: async_sessionmaker[AsyncSession],
        settings: Settings,
        # Accepted for Protocol conformance; inert here — no service-wide
        # gcloud identity exists to fall back to.
        allow_service_default: bool = False,
    ) -> str:
        if agent_id is None:
            # CLI-minted JWTs may carry no agent_id; per-agent
            # impersonation needs one. Fail closed.
            raise NoBindingError(
                "gcloud provider requires an agent_id; the calling JWT carried none"
            )
        async with sessionmaker() as session:
            binding = await get_agent_google_binding(session, agent_id=agent_id)
        if binding is None:
            raise NoBindingError(
                "This agent has no Google identity bound. Google access is "
                "configured per deployment by the operator running it, not "
                "from chat — tell them to run `daimon agents bind-google "
                "<agent> <email> --scopes <scope>...` to bind one."
            )
        user_email = settings.credentials.google_user_email
        if (
            user_email is not None
            and binding.email.casefold() == user_email.casefold()
            and settings.credentials.google_user_json is not None
        ):
            return await asyncio.to_thread(
                _refresh_user_token,
                settings.credentials.google_user_json.get_secret_value(),
                list(binding.scopes),
            )
        if settings.credentials.google_sa_json is None:
            raise ProviderConfigError(
                "gcloud provider requires settings.credentials.google_sa_json, or "
                "settings.credentials.google_user_json with "
                "settings.credentials.google_user_email matching the agent binding"
            )
        sa_json_text = settings.credentials.google_sa_json.get_secret_value()
        try:
            decoded: Any = json.loads(sa_json_text)
        except json.JSONDecodeError as e:
            raise ProviderConfigError(
                "settings.credentials.google_sa_json is not valid JSON "
                "(must be the full SA JSON blob, not a filesystem path)"
            ) from e
        if not isinstance(decoded, dict):
            raise ProviderConfigError(
                "settings.credentials.google_sa_json must decode to a JSON object"
            )
        info = cast(dict[str, Any], decoded)
        if info.get("type") != "service_account":
            raise ProviderConfigError(
                "settings.credentials.google_sa_json missing 'type=service_account' field"
            )
        # google-auth lacks complete stubs for from_service_account_info /
        # Credentials.refresh / Credentials.token (#typing skill: targeted
        # ignores with reasons rather than `cast(Any, ...)`).
        creds = service_account.Credentials.from_service_account_info(  # pyright: ignore[reportUnknownMemberType]
            info,
            scopes=list(binding.scopes),
            subject=binding.email,
        )
        # Sync HTTP call — wrap in to_thread so the FastMCP event loop
        # is not blocked while the Google token endpoint round-trips.
        await asyncio.to_thread(
            creds.refresh,  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
            Request(),
        )
        raw_token = cast(str | None, creds.token)  # pyright: ignore[reportUnknownMemberType]
        if raw_token is None:
            raise ProviderConfigError("google-auth refresh returned no token")
        return raw_token


def _refresh_user_token(json_text: str, scopes: list[str]) -> str:
    """Validate and refresh without exposing credential values in SDK errors."""
    # Do not chain JSON/SDK exceptions: they can contain credential values.
    try:
        decoded: Any = json.loads(json_text)
    except json.JSONDecodeError:
        decoded = None
    if not isinstance(decoded, dict):
        raise ProviderConfigError(
            "settings.credentials.google_user_json must be a valid JSON object"
        )
    info = cast(dict[str, Any], decoded)
    if info.get("type") != "authorized_user":
        raise ProviderConfigError(
            "settings.credentials.google_user_json requires type=authorized_user"
        )
    fields = ("client_id", "client_secret", "refresh_token")
    if any(not isinstance(info.get(key), str) or not info[key].strip() for key in fields):
        raise ProviderConfigError(
            "settings.credentials.google_user_json requires nonempty string fields: "
            "client_id, client_secret, refresh_token"
        )
    try:
        # Ignore optional input tokens, scopes and endpoints: the binding controls
        # scopes and google-auth supplies Google's OAuth token endpoint.
        creds = credentials.Credentials.from_authorized_user_info(  # pyright: ignore[reportUnknownMemberType]
            {key: info[key] for key in fields}, scopes=scopes
        )
        creds.refresh(Request())  # pyright: ignore[reportUnknownMemberType]
        token = cast(str | None, creds.token)  # pyright: ignore[reportUnknownMemberType]
    except Exception:
        # Raise outside the handler so even the exception context stays secret-free.
        pass
    else:
        if token:
            return token
    raise ProviderConfigError(
        "settings.credentials.google_user_json could not refresh a Google access token"
    )
