"""Delete a purged account's sessions at the other model providers, best effort.

Runs after the erasure commits, so a failure is logged and never undoes it. Only
OpenAI sessions can be deleted: the Gemini driver has no session delete, so its
stored interactions stay for Google's own retention, which the privacy policy says.
Each session is deleted with the key and project header it was created under
(`usage_transport`); a session whose key cannot be resolved is left and logged.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable, Iterable

import structlog
from daimon.core.stores.mux_erasure import ProviderSession
from mux.contracts.ids import ResourceRef, Scope
from mux.drivers.openai import (
    MemoryRecoveryJournal,
    MemoryUsageRevisions,
    OpenAIDriver,
)
from mux.errors import MuxError
from mux.profiles import MANAGED_AGENTS, PERSISTENT_WORKSPACE

OPENAI = PERSISTENT_WORKSPACE.provider

log = structlog.get_logger(__name__)

#: Deletes the given provider sessions; returns how many were deleted.
ProviderSessionDeleter = Callable[[Iterable[ProviderSession]], Awaitable[int]]


def _only(scope: Scope) -> Callable[[Scope, str, str | None], bool]:
    """The driver acts for this one tenant's erasure and nothing else."""
    return lambda asked, _kind, _id: asked == scope


async def delete_provider_sessions(sessions: Iterable[ProviderSession]) -> int:
    """Delete each OpenAI session through the driver's Sessions port; log the rest."""
    from daimon.core.turn.openai_deployment import usage_transport

    deleted = 0
    for item in sessions:
        if item.provider == MANAGED_AGENTS.provider:
            continue  # the Managed Agents sweep in `purge_account` deletes these
        if item.provider == OPENAI and item.account_scope_id is None:
            log.warning("purge.provider_session_unscoped", tenant_id=str(item.tenant_id))
            continue
        transport = (
            usage_transport(str(item.tenant_id), item.profile, item.account_scope_id)
            if item.provider == OPENAI and item.account_scope_id is not None
            else None
        )
        if transport is None or item.account_scope_id is None:
            log.info(
                "purge.provider_session_kept", provider=item.provider, tenant_id=str(item.tenant_id)
            )
            continue
        scope = Scope(
            tenant_id=str(item.tenant_id),
            account_id="erased",
            principal_id="daimon",
            authorization_id="account-erasure",
        )
        driver = OpenAIDriver(
            transport,
            account_scope_id=item.account_scope_id,
            profile_id=item.profile,
            authorization=_only(scope),
            journal=MemoryRecoveryJournal(),
            usage_revisions=MemoryUsageRevisions(),
        )
        ref = ResourceRef(
            id=item.session_id,
            kind="session",
            provider=OPENAI,
            account_scope_id=item.account_scope_id,
            tenant_id=str(item.tenant_id),
            account_id=scope.account_id,
        )
        try:
            await driver.sessions.delete(
                scope,
                ref,
                key=f"privacy-delete:{uuid.uuid5(item.tenant_id, item.session_id)}",
            )
        except MuxError as error:
            log.warning(
                "purge.provider_session_failed",
                provider=item.provider,
                tenant_id=str(item.tenant_id),
                error=type(error).__name__,
            )
            continue
        deleted += 1
    return deleted
