"""Headless MCP turn metering backfill.

Agent-chat `start_turn` (over MCP) creates an MA session and sends a
user.message but never drives the SSE stream, so the live `record_turn_usage`
hook in the turn driver / headless runner never fires — those sessions produce
no usage_events / tenant_ledger rows even though the real Anthropic cost hit
the operator's shared key. This sweep closes that gap out of band: it lists MA
sessions, folds each session's `span.model_request_end` events, and replays
them through `record_turn_usage`.

`record_turn_usage` is idempotent on (managed_session_id, event_id) — the same
grain the live paths write — so the sweep can run every tick over the whole
workspace and already-recorded sessions (Discord, scheduler, and previously
swept headless ones) are pure no-ops. No need to distinguish "headless-only"
sessions.

Attribution comes off the metadata `create_session` stamps on every session:
`daimon_tenant` is the billed tenant (the tenant_ledger debit keys on it) and
`daimon_account` resolves to the owning human's platform_user_id for per-member
usage reporting.

Per `guideline:architecture` Error Propagation: this does not swallow
exceptions — the scheduler tick is the boundary that decides a sweep failure
must not kill the loop.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

from anthropic import AsyncAnthropic
from daimon.core.defaults.metadata import MA_METADATA_KEY_ACCOUNT, MA_METADATA_KEY_TENANT
from daimon.core.pricing import MODEL_PRICING
from daimon.core.stores.accounts import get_account_with_tenant
from daimon.core.stores.tenants import list_all_tenant_ids
from daimon.core.usage_recording import record_turn_usage
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


async def sweep_headless_usage(
    client: AsyncAnthropic,
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    markup: Decimal,
) -> int:
    """Fold span.model_request_end events from all tagged MA sessions into usage.

    Returns the number of events replayed through `record_turn_usage` (including
    idempotent no-ops). Sessions with no `daimon_tenant` tag are skipped — they
    aren't billable Daimon turns (e.g. DMs or foreign sessions).

    ponytail: full workspace scan every call; bounded only by record_turn_usage's
    idempotency. Add a `created_at_gte` watermark to sessions.list when session
    volume makes re-reading every session's events too costly.
    """
    recorded = 0
    async with sessionmaker() as s:
        known_tenants = await list_all_tenant_ids(s)
    async for session in client.beta.sessions.list():
        tenant_raw = session.metadata.get(MA_METADATA_KEY_TENANT)
        if tenant_raw is None:
            continue
        tenant_id = uuid.UUID(tenant_raw)
        if tenant_id not in known_tenants:
            # Session belongs to a tenant this deployment doesn't own. A shared MA
            # workspace holds sessions from other deployments/evals whose tenant_ids
            # are absent from this DB; recording them violates usage_events' FK and
            # is meaningless (not our tenant to bill). Skip.
            continue
        platform_user_id = await _resolve_platform_user_id(sessionmaker, session.metadata)
        model_id = session.agent.model.id
        pricing = MODEL_PRICING.get(model_id)

        async for event in client.beta.sessions.events.list(session.id, order="asc"):
            if event.type != "span.model_request_end":
                continue
            await record_turn_usage(
                sessionmaker=sessionmaker,
                tenant_id=tenant_id,
                platform_user_id=platform_user_id,
                managed_session_id=session.id,
                model_id=model_id,
                event=event,
                markup=markup,
                pricing=pricing,
            )
            recorded += 1
    return recorded


async def _resolve_platform_user_id(
    sessionmaker: async_sessionmaker[AsyncSession],
    metadata: dict[str, str],
) -> str | None:
    """Resolve the owning human's platform_user_id from the session's daimon_account.

    Returns None when the session has no account tag or the account has no
    discord principal — the tenant_ledger debit still fires on tenant_id alone,
    so billing stays correct; only per-member reporting attribution is absent.
    """
    account_raw = metadata.get(MA_METADATA_KEY_ACCOUNT)
    if account_raw is None:
        return None
    async with sessionmaker() as s:
        identity = await get_account_with_tenant(s, account_id=uuid.UUID(account_raw))
    return identity.platform_user_id if identity is not None else None
