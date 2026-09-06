"""Wire shape of the tenant map a hub login bakes into its token.

The OAuth proxy embeds whatever ``_extract_upstream_claims`` returns under the
``upstream_claims`` key of the issued JWT and surfaces it again as
``AccessToken.claims["upstream_claims"]`` on every request. Both providers
encode with ``encode_hub_claims``; the middleware decodes with
``decode_hub_claims``. Anything malformed decodes to ``None`` so a token from
an older deployment fails closed rather than half-parsing.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from typing import Any, cast, get_args

from daimon.adapters.mcp.hub.identity import HubIdentity
from daimon.core.hub_identity import HubTenant
from daimon.core.stores.domain import Platform

UPSTREAM_CLAIMS_KEY = "upstream_claims"
_PLATFORMS: frozenset[str] = frozenset(get_args(Platform))


def encode_hub_claims(
    *, platform: Platform, platform_user_id: str, tenants: Sequence[HubTenant]
) -> dict[str, Any]:
    return {
        "platform": platform,
        "platform_user_id": platform_user_id,
        "tenants": [
            {
                "tenant_id": str(t.tenant_id),
                "account_id": str(t.account_id),
                "workspace_id": t.workspace_id,
                "workspace_name": t.workspace_name,
            }
            for t in tenants
        ],
    }


def decode_hub_claims(claims: Mapping[str, Any]) -> HubIdentity | None:
    raw = claims.get(UPSTREAM_CLAIMS_KEY)
    if not isinstance(raw, Mapping):
        return None
    raw = cast("Mapping[str, Any]", raw)
    platform = raw.get("platform")
    user = raw.get("platform_user_id")
    tenants_raw = raw.get("tenants")
    if platform not in _PLATFORMS or not isinstance(user, str) or not isinstance(tenants_raw, list):
        return None
    tenants: list[HubTenant] = []
    for entry in cast("list[object]", tenants_raw):
        if not isinstance(entry, Mapping):
            return None
        entry = cast("Mapping[str, Any]", entry)
        try:
            tenants.append(
                HubTenant(
                    tenant_id=uuid.UUID(str(entry["tenant_id"])),
                    account_id=uuid.UUID(str(entry["account_id"])),
                    workspace_id=str(entry["workspace_id"]),
                    workspace_name=str(entry["workspace_name"]),
                )
            )
        except (KeyError, ValueError):
            return None
    return HubIdentity(
        platform=cast(Platform, platform), platform_user_id=user, tenants=tuple(tenants)
    )
