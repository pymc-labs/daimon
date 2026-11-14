"""Write the selfhosted records a grant names; print the grant facts as JSON.

Runs inside the MCP pod (`register.sh`). The CLI operator grant is always
written. Teams grants are written only when `TEAMS_GRANTS` is set: a JSON
object mapping each Teams conversation (a channel ID such as
`19:...@thread.tacv2`, a group chat ID, or a person's 1:1 chat ID) to the
Entra object IDs of the people who may get model replies there. The Teams
tenant is the pod's own `DAIMON_TEAMS__TENANT_ID`, so it always matches the
tenant the Teams host admits turns for. Nothing here is a credential.
"""

import asyncio
import json
import os
import uuid
from decimal import Decimal

from daimon.core.channel_backend import channel_ref, set_channel_backend
from daimon.core.defaults.provisioning import provision_tenant
from daimon.core.microsoft_identity import canonical_uuid
from daimon.core.scope import TenantScopeRef
from daimon.core.stores.access_policy import (
    load_access_policy,
    policy_write_transaction,
    set_access_policy,
)
from daimon.core.stores.identity import get_or_create_platform_principal
from daimon.core.stores.scoped_config_write import set_fields
from mux.contracts.config import BackendConfig
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine


async def _enable_selfhosted(s: AsyncSession, tenant_id: uuid.UUID) -> None:
    """Point the tenant at the seeded agent and add selfhosted to its enabled backends."""
    policy = await load_access_policy(s, tenant_id=tenant_id)
    await set_fields(
        s,
        scope=TenantScopeRef(tenant_id=tenant_id),
        tenant_id=tenant_id,
        agent_name="daimon",
        environment_name="default",
    )
    await set_access_policy(
        s,
        tenant_id=tenant_id,
        policy=policy.model_copy(
            update={
                "enabled_backends": tuple(dict.fromkeys((*policy.enabled_backends, "selfhosted")))
            }
        ),
    )


async def _select_selfhosted(
    s: AsyncSession, tenant_id: uuid.UUID, platform: str, channel_id: str, model: str
) -> str:
    """Make the channel's turns use the selfhosted loop; return the config digest."""
    revision = await set_channel_backend(
        s,
        channel_ref(tenant_id, platform, channel_id),
        BackendConfig(backend="selfhosted", profile="selfhosted.loop", model=model),
    )
    return revision.content_digest()


async def register(sf, model):
    tenant = await provision_tenant(
        sf, platform="cli", workspace_id="aks-selfhost", signup_credit=Decimal("2")
    )
    async with policy_write_transaction(sf, tenant_id=tenant.tenant_id) as s:
        caller = await get_or_create_platform_principal(
            s, tenant_id=tenant.tenant_id, platform="cli", external_id="operator"
        )
        await _enable_selfhosted(s, tenant.tenant_id)
        digest = await _select_selfhosted(s, tenant.tenant_id, "cli", "selfhosted-operator", model)
    return {
        "tenant_id": str(tenant.tenant_id),
        "account_id": str(caller.account_id),
        "config_digest": digest,
    }


class _JsonObject(list):
    """A JSON object's key/value pairs in order, duplicate keys kept."""


def parse_teams_grants(raw: str) -> dict[str, tuple[str, ...]]:
    """Validate `TEAMS_GRANTS`: conversation ID -> Entra object IDs, in Teams' own forms.

    A channel link's thread suffix (`;messageid=...`) is dropped, as the Teams
    host keys a channel by the ID before it. Person IDs are lowercased UUIDs,
    the form the Teams host records a sender under. A conversation listed twice,
    as itself or with another suffix, is refused rather than merged.
    """
    shape = "TEAMS_GRANTS must be a JSON object of conversation ID -> person IDs"
    try:
        pairs = json.loads(raw, object_pairs_hook=_JsonObject)
    except json.JSONDecodeError as error:
        raise SystemExit(f"{shape}: {error}") from None
    if type(pairs) is not _JsonObject or not pairs:
        raise SystemExit(shape)
    parsed: dict[str, tuple[str, ...]] = {}
    for conversation, people in pairs:
        channel_id = str(conversation).split(";", 1)[0].strip()
        if not channel_id:
            raise SystemExit("TEAMS_GRANTS has an empty conversation ID")
        if channel_id in parsed:
            raise SystemExit(
                f"TEAMS_GRANTS lists conversation {channel_id!r} twice; list its people once"
            )
        if type(people) is not list or not people:
            raise SystemExit(f"TEAMS_GRANTS[{channel_id!r}] must list at least one person ID")
        ids: list[str] = []
        for person in people:
            canonical = canonical_uuid(person) if isinstance(person, str) else None
            if canonical is None:
                raise SystemExit(
                    f"TEAMS_GRANTS[{channel_id!r}]: {person!r} is not an Entra object ID"
                )
            ids.append(canonical)
        parsed[channel_id] = tuple(dict.fromkeys(ids))
    return parsed


async def register_teams(sf, model, entra_tenant_id, grants):
    tenant = await provision_tenant(sf, platform="teams", workspace_id=entra_tenant_id)
    tenant_id = tenant.tenant_id
    rows = []
    async with policy_write_transaction(sf, tenant_id=tenant_id) as s:
        await _enable_selfhosted(s, tenant_id)
        for channel_id, people in grants.items():
            digest = await _select_selfhosted(s, tenant_id, "teams", channel_id, model)
            for person in people:
                principal = await get_or_create_platform_principal(
                    s, tenant_id=tenant_id, platform="teams", external_id=person
                )
                rows.append(
                    {
                        "channel_id": channel_id,
                        "account_id": str(principal.account_id),
                        "config_digest": digest,
                    }
                )
    return {"tenant_id": str(tenant_id), "grants": rows}


async def provision(sf, environ):
    model = environ["SELFHOSTED_MODEL"]
    raw = environ.get("TEAMS_GRANTS", "").strip()
    entra_tenant_id = environ.get("DAIMON_TEAMS__TENANT_ID", "").strip()
    # Checked before anything is written, so a bad map changes no grant.
    grants = parse_teams_grants(raw) if raw else None
    if grants and not entra_tenant_id:
        raise SystemExit("TEAMS_GRANTS needs DAIMON_TEAMS__TENANT_ID in the host Secret")
    registration = await register(sf, model)
    if grants:
        registration["teams"] = await register_teams(sf, model, entra_tenant_id, grants)
    return registration


async def main():
    e = create_async_engine(os.environ["DAIMON_DATABASE__URL"])
    try:
        registration = await provision(async_sessionmaker(e, expire_on_commit=False), os.environ)
    finally:
        await e.dispose()
    print(json.dumps(registration))


if __name__ == "__main__":
    asyncio.run(main())
