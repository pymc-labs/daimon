"""What a web sign-in proves to the data store: the person's own platform account there.

A Discord sign-in is the tenant's own platform user (`identity.mirror_platform`); a Slack or
Microsoft sign-in is that workspace's user (`identity.sign_in_proof`). Only workspaces the
sign-in vouched for, and only with data sources on there. A failure or a slow store never
fails the sign-in: the link waits for the next one.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence

import structlog
from daimon.adapters.web.caller import workspace_data_sources_on
from daimon.adapters.web.data_common import schemas_of, store
from daimon.adapters.web.runtime import WebRuntime
from daimon.core.ports.gate import data_sources_on
from daimon.core.stores.domain import WebPersonRow
from daimon.core.stores.tenants import get_tenant
from daimon.core.web_identity import PLATFORM_OF, VerifiedIdentity
from daimon.data import identity as data_identity
from daimon.data import pool

log = structlog.get_logger()
#: How long one person's link may take before the sign-in goes on without it.
_SECONDS = 3


async def prove_sign_in(
    runtime: WebRuntime, identity: VerifiedIdentity, people: Sequence[WebPersonRow]
) -> None:
    """Link `identity` as each of `people`'s source identity in the workspace it proved, once
    the store is ready, so a person who signs in before the first source is known by it."""
    if not data_sources_on(runtime.settings):
        return
    proved = {workspace for workspace, _ in identity.workspaces}
    for person in people:
        try:
            async with asyncio.timeout(_SECONDS):
                async with runtime.sessionmaker() as db:
                    tenant = await get_tenant(db, person.tenant_id)
                    if (
                        tenant is None
                        or tenant.platform != PLATFORM_OF[identity.provider]
                        or tenant.external_id not in proved
                        or not await workspace_data_sources_on(runtime, db, tenant.id)
                        or not await pool.ready(runtime.settings)
                    ):
                        continue
                async with store(runtime) as conn:
                    schemas, user = schemas_of(runtime), identity.chat_user_id
                    if identity.provider == "discord":
                        await data_identity.mirror_platform(
                            conn, schemas, tenant_id=tenant.id, platform="discord",
                            workspace=tenant.external_id, people={user: person.account_id},
                        )  # fmt: skip
                    else:
                        proof = data_identity.sign_in_proof(
                            identity.provider, tenant.external_id, user
                        )
                        await data_identity.link(
                            conn, schemas, tenant_id=tenant.id, account_id=person.account_id,
                            proof=proof,
                        )  # fmt: skip
        except Exception as e:
            log.warning(
                "data.sign_in_link_failed", provider=identity.provider, why=type(e).__name__
            )
