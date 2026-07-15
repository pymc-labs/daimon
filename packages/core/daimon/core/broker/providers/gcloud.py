"""GWS provider — service-account impersonation via google-auth (Phase 19 D-15..D-18).

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
from google.oauth2 import service_account
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


class GcloudTokenProvider:
    """Mints a Google access token by impersonating ``binding.email`` with
    the tenant Service Account, using DWD on the bound scopes."""

    service: ClassVar[str] = "gcloud"

    async def mint_token(
        self,
        *,
        account_id: uuid.UUID,
        agent_id: uuid.UUID | None,
        sessionmaker: async_sessionmaker[AsyncSession],
        settings: Settings,
    ) -> str:
        # Pitfall 2: config check FIRST, then binding check.
        if settings.credentials.google_sa_json is None:
            raise ProviderConfigError(
                "gcloud provider requires settings.credentials.google_sa_json"
            )
        if agent_id is None:
            # Pitfall 7: CLI-minted JWTs may carry no agent_id; per-agent
            # impersonation needs one. Fail closed (T-19-03-02).
            raise NoBindingError(
                "gcloud provider requires an agent_id; the calling JWT carried none"
            )
        async with sessionmaker() as session:
            binding = await get_agent_google_binding(session, agent_id=agent_id)
        if binding is None:
            raise NoBindingError(
                "Agent not bound to a Google identity — operator must configure via agent-setup."
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
        # is not blocked while the Google token endpoint round-trips (T-19-03-05).
        await asyncio.to_thread(
            creds.refresh,  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
            Request(),
        )
        raw_token = cast(str | None, creds.token)  # pyright: ignore[reportUnknownMemberType]
        if raw_token is None:
            raise ProviderConfigError("google-auth refresh returned no token")
        return raw_token
