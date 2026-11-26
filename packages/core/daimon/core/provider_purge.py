"""Delete a purged account's sessions at the other model providers, best effort.

Runs after the erasure commits, so a failure is logged and never undoes it. Only
OpenAI sessions can be deleted: the Gemini driver has no session delete, so its
stored interactions stay for Google's own retention, which the privacy policy says.
Each session is deleted with the key and project header it was created under
(`usage_transport`), with the keys file it mounted; a session whose key cannot be
resolved is left and logged.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable, Iterable

import structlog
from daimon.core.channel_backend import backend_refusal_causes
from daimon.core.stores.mux_erasure import ProviderSession
from daimon.core.turn.replace_binding import ENV_FILE_REF, delete_env_file
from mux.contracts.ids import ResourceRef, Scope
from mux.contracts.resources import ProviderBinding
from mux.drivers.openai import (
    MemoryRecoveryJournal,
    MemoryUsageRevisions,
    OpenAIDriver,
)
from mux.errors import MuxError
from mux.profiles import MANAGED_AGENTS, PERSISTENT_WORKSPACE
from mux.state.store import SESSION_REF

OPENAI = PERSISTENT_WORKSPACE.provider

log = structlog.get_logger(__name__)

#: Deletes the given provider sessions; returns how many were deleted.
ProviderSessionDeleter = Callable[[Iterable[ProviderSession]], Awaitable[int]]


def _only(scope: Scope) -> Callable[[Scope, str, str | None], bool]:
    """The driver acts for this one tenant's erasure and nothing else."""
    return lambda asked, _kind, _id: asked == scope


def _driver(
    tenant_id: uuid.UUID, profile: str, account_scope_id: str, session_id: str
) -> tuple[Scope, OpenAIDriver, ResourceRef] | None:
    """An OpenAI driver for the session's own key and project, or None when unresolved."""
    from daimon.core.turn.openai_deployment import usage_transport

    transport = usage_transport(str(tenant_id), profile, account_scope_id)
    if transport is None:
        return None
    scope = Scope(
        tenant_id=str(tenant_id),
        account_id="erased",
        principal_id="daimon",
        authorization_id="account-erasure",
    )
    driver = OpenAIDriver(
        transport,
        account_scope_id=account_scope_id,
        profile_id=profile,
        authorization=_only(scope),
        journal=MemoryRecoveryJournal(),
        usage_revisions=MemoryUsageRevisions(),
    )
    ref = ResourceRef(
        id=session_id,
        kind="session",
        provider=OPENAI,
        account_scope_id=account_scope_id,
        tenant_id=str(tenant_id),
        account_id=scope.account_id,
    )
    return scope, driver, ref


async def delete_released_env_file(tenant_id: uuid.UUID, binding: ProviderBinding) -> None:
    """Delete the keys file of the OpenAI session a thread release just let go.

    The session stays for the next provider to read (`turn.carry_over`), but nothing
    mounts its keys again, so they do not stay at the provider. Best effort, logged.
    """
    refs = binding.native_refs
    scope_id, session_id = refs.get("account_scope"), refs.get(SESSION_REF)
    if binding.provider != OPENAI or not scope_id or not session_id or ENV_FILE_REF not in refs:
        return
    opened = _driver(tenant_id, binding.profile, scope_id, session_id)
    if opened is None:
        log.info("release.env_file_kept", tenant_id=str(tenant_id))
        return
    scope, driver, ref = opened
    key = f"release-env:{uuid.uuid5(tenant_id, session_id)}"
    await delete_env_file(scope, driver, ref, refs, key=key)


async def delete_provider_sessions(sessions: Iterable[ProviderSession]) -> int:
    """Delete each OpenAI session through the driver's Sessions port; log the rest."""
    deleted = 0
    for item in sessions:
        if item.provider == MANAGED_AGENTS.provider:
            continue  # the Managed Agents sweep in `purge_account` deletes these
        if item.provider == OPENAI and item.account_scope_id is None:
            log.warning("purge.provider_session_unscoped", tenant_id=str(item.tenant_id))
            continue
        opened = (
            _driver(item.tenant_id, item.profile, item.account_scope_id, item.session_id)
            if item.provider == OPENAI and item.account_scope_id is not None
            else None
        )
        if opened is None:
            log.info(
                "purge.provider_session_kept", provider=item.provider, tenant_id=str(item.tenant_id)
            )
            continue
        scope, driver, ref = opened
        key = f"privacy-delete:{uuid.uuid5(item.tenant_id, item.session_id)}"
        try:
            await driver.sessions.delete(scope, ref, key=key)
        except MuxError as error:
            log.warning(
                "purge.provider_session_failed",
                provider=item.provider,
                tenant_id=str(item.tenant_id),
                error=type(error).__name__,
                causes=backend_refusal_causes(error),
            )
            continue
        finally:
            # No keys stay at the provider, whether or not the session went.
            await delete_env_file(scope, driver, ref, item.native_refs, key=key)
        deleted += 1
    return deleted
