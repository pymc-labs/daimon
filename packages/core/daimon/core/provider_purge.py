"""Delete a purged account's sessions at the other model providers, best effort.

Runs after the erasure commits, so a failure is logged and never undoes it. Only
OpenAI sessions can be deleted: the Gemini driver has no session delete, so its
stored interactions stay for Google's own retention, which the privacy policy says.
Uses the deployment's key (`daimon.core.provider_keys`); a tenant-only key is not
read here, and its sessions are left and logged.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Awaitable, Callable, Iterable

import structlog
from daimon.core.provider_keys import DEFAULT_KEY_ENV, provider_key_present
from daimon.core.stores.mux_erasure import ProviderSession
from mux.contracts.ids import ResourceRef, Scope
from mux.drivers.openai import (
    MemoryRecoveryJournal,
    MemoryUsageRevisions,
    OpenAIDriver,
    configured_transport,
)
from mux.errors import MuxError
from mux.profiles import MANAGED_AGENTS, PERSISTENT_WORKSPACE

OPENAI = PERSISTENT_WORKSPACE.provider

log = structlog.get_logger(__name__)

#: Deletes the given provider sessions; returns how many were deleted.
ProviderSessionDeleter = Callable[[Iterable[ProviderSession]], Awaitable[int]]


async def delete_provider_sessions(sessions: Iterable[ProviderSession]) -> int:
    """Delete each OpenAI session through the driver's Sessions port; log the rest."""
    deleted = 0
    for item in sessions:
        if item.provider == MANAGED_AGENTS.provider:
            continue  # the Managed Agents sweep in `purge_account` deletes these
        if item.provider != OPENAI or not provider_key_present(OPENAI):
            log.info(
                "purge.provider_session_kept", provider=item.provider, tenant_id=str(item.tenant_id)
            )
            continue
        if item.account_scope_id is None:
            log.warning("purge.provider_session_unscoped", tenant_id=str(item.tenant_id))
            continue
        driver = OpenAIDriver(
            configured_transport(api_key=os.environ[DEFAULT_KEY_ENV[OPENAI]], project=None),
            account_scope_id=item.account_scope_id,
            journal=MemoryRecoveryJournal(),
            usage_revisions=MemoryUsageRevisions(),
        )
        ref = ResourceRef(
            id=item.session_id,
            kind="session",
            provider=OPENAI,
            account_scope_id=item.account_scope_id,
            tenant_id=str(item.tenant_id),
            account_id="erased",
        )
        try:
            await driver.sessions.delete(
                Scope.platform(reason="account erasure"),
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
