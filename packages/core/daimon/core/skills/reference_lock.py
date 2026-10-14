"""`guarded_agent_update`, kept only for callers that still import it.

Skill deletes check for references in one place
(`daimon.core.skills.attachments.delete_skill_unless_attached`); attaches
take no lock. Remove this module once nothing imports it.
"""

from __future__ import annotations

from typing import Any

from anthropic import AsyncAnthropic
from anthropic.types.beta import BetaManagedAgentsAgent
from daimon.core.mux_compat import update_agent
from mux.contracts.ids import Scope


async def guarded_agent_update(
    client: AsyncAnthropic,
    _scope: object,
    fresh: BetaManagedAgentsAgent,
    *,
    backend_scope: Scope | None = None,
    **fields: Any,  # noqa: ANN401
) -> BetaManagedAgentsAgent:
    """Update `fresh` to `fields` at `fresh.version`; `_scope` is ignored.

    `backend_scope` is the tenant scope of the update; without it the update
    runs under the legacy host scope.
    """
    backend = backend_scope or Scope.legacy_host_authorized(
        call_site="daimon.core.skills.reference_lock:guarded_agent_update"
    )
    return await update_agent(
        client, fresh.id, version=fresh.version, payload=fields, scope=backend
    )


__all__ = ["guarded_agent_update"]
