"""Deploy smoke-check orchestrator.

`run_smoke_check` proves a fresh deploy can actually bill a turn: it
provisions (or reconciles) a dedicated `("cli", "smoke")` tenant, runs one
billed headless agent turn, and raises `SmokeCheckError` unless at least one
`usage_events` row was written for that tenant since the run started. A green
run is evidence the whole spend path — MA session creation, SSE draining,
`span.model_request_end` handling, and the `usage_events` write — is live in
the deployed environment. Everything downstream (the `daimon smoke` CLI, the
`deploy.yml` gate step) is a thin wrapper over this function.

Start-timestamp rationale: the run's "since" boundary is read from the
**database** clock (`select(func.now())`), not the app container's wall
clock. `UsageEvent.occurred_at` is a Postgres `server_default=func.now()`
value, so comparing it against an app-clock timestamp would make the
pass/fail assertion sensitive to VM/Cloud-SQL clock skew. This is a clock
read, not a domain query, so it does not touch `daimon.core._models`.

A smoke turn passes only its agent/environment IDs, prompt, usage recorder,
database session factory, tenant ID, and the per-session `SMOKE_MODEL` override to
`headless_runner.run_turn`. Every other optional keyword (vault/MCP settings,
the caller account ID, per-agent UUID, crypto key,
and GitHub credentials) keeps its default. The model override changes this
session only; reconciliation leaves the smoke agent's default model alone.
A smoke turn never touches vault, MCP, or GitHub code paths; it only proves
the MA and billing plumbing works. For the same reason the smoke tenant's
defaults are reconciled with `public_url=None`:
a non-None public_url makes the defaults merge attach the `daimon-mcp`
server to the seeded agent, and MA refuses to start a session for such an
agent without a vault credential — which a bare turn never binds.

No-retries posture: per `guideline:architecture` error propagation, this
orchestrator does not retry or swallow. The only exception this module
catches is `TimeoutError` around the turn itself, which it re-raises as a
`SmokeCheckError` naming the timeout; every other exception (resolver
misses, MA API errors, DB errors, the turn's own `RuntimeError` on
`session.error`) propagates unchanged to the caller.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from decimal import Decimal
from functools import partial
from pathlib import Path

from anthropic import AsyncAnthropic
from daimon.core.defaults.provisioning import provision_tenant, reconcile_tenant_defaults
from daimon.core.errors import DaimonError
from daimon.core.headless_runner import run_turn
from daimon.core.ma_resolver import ResolverCache, resolve_agent, resolve_environment
from daimon.core.pricing import MODEL_PRICING
from daimon.core.stores import tenant_ledger, usage_events
from daimon.core.stores.domain import Platform
from daimon.core.usage_recording import record_turn_usage
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

SMOKE_PLATFORM: Platform = "cli"
"""D-05's dedicated smoke identity: never routed through discover_tenant()."""

SMOKE_WORKSPACE_ID = "smoke"

SMOKE_PROMPT = "Reply with the single word: done"
"""SPEC req 4: a fixed, trivial, no-tools message bounding per-run spend."""

SMOKE_MODEL = "claude-haiku-5-5"
"""Keep the deployment smoke turn inexpensive without changing seeded agents."""

SMOKE_TOPUP_USD = Decimal("5.00")
"""D-07's small top-up credit, applied only when the ledger balance is <= 0."""

SMOKE_TIMEOUT_S = 180.0
"""D-04's in-command bound; well under the scheduler's 600s dispatch_timeout_s
reference, since a no-tool turn is far cheaper."""


class SmokeCheckError(DaimonError):
    """The smoke check could not prove a billed turn completed successfully."""


class SmokeResult(BaseModel):
    """Pydantic projection returned by run_smoke_check. Primitives only."""

    tenant_id: uuid.UUID
    account_id: uuid.UUID
    agent_id: str
    environment_id: str
    topped_up: bool
    usage_event_count: int
    tail: str


async def run_smoke_check(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    anthropic: AsyncAnthropic,
    defaults_root: Path,
    agent_tag: str,
    environment_tag: str,
    resolver_cache: ResolverCache,
    topup_key: str,
    markup: Decimal,
    timeout_s: float = SMOKE_TIMEOUT_S,
    on_stage: Callable[[str], None],
) -> SmokeResult:
    """Provision the smoke tenant, run one billed bare turn, assert it billed.

    Raises `SmokeCheckError` if the turn times out or if no `usage_events`
    row exists for the smoke tenant since the run began. Every other failure
    (resolver miss, MA API error, DB error, a `session.error` turn failure)
    propagates unchanged — see the module docstring's "No-retries posture".
    """
    on_stage("provisioning smoke tenant")
    provisioned = await provision_tenant(
        session_factory, platform=SMOKE_PLATFORM, workspace_id=SMOKE_WORKSPACE_ID
    )
    tenant_id = provisioned.tenant_id
    account_id = provisioned.account_id
    on_stage(f"smoke tenant ready: tenant_id={tenant_id} account_id={account_id}")

    async with session_factory() as s, s.begin():
        started_at = (await s.execute(select(func.now()))).scalar_one()
        balance = await tenant_ledger.get_balance(s, tenant_id=tenant_id)
        topped_up = False
        if balance <= Decimal("0"):
            topped_up = await tenant_ledger.insert_entry(
                s,
                tenant_id=tenant_id,
                delta_usd=SMOKE_TOPUP_USD,
                reason="smoke-topup",
                idempotency_key=f"smoke-topup:{topup_key}",
            )
    on_stage(f"balance checked: balance={balance} topped_up={topped_up}")

    async def _apply() -> object:
        return await reconcile_tenant_defaults(
            # public_url=None keeps the seeded smoke agent free of the
            # daimon-mcp server — a bare turn binds no vault credential, and
            # MA fails session init for an agent whose MCP server has none.
            anthropic,
            session_factory,
            defaults_root,
            tenant_id=tenant_id,
            public_url=None,
        )

    on_stage(f"resolving agent tag={agent_tag!r}")
    resolved_agent_id = await resolve_agent(
        anthropic,
        tenant_id=tenant_id,
        daimon_tag=agent_tag,
        cached_id=None,
        apply_callable=_apply,
        cache=resolver_cache,
    )
    on_stage(f"resolving environment tag={environment_tag!r}")
    resolved_environment_id = await resolve_environment(
        anthropic,
        tenant_id=tenant_id,
        daimon_tag=environment_tag,
        cached_id=None,
        apply_callable=_apply,
        cache=resolver_cache,
    )
    on_stage(f"resolved agent_id={resolved_agent_id} environment_id={resolved_environment_id}")

    def usage_record_factory(session_id: str, model_id: str) -> Callable[..., Awaitable[None]]:
        return partial(
            record_turn_usage,
            sessionmaker=session_factory,
            platform_user_id=None,
            managed_session_id=session_id,
            model_id=model_id,
            tenant_id=tenant_id,
            markup=markup,
            pricing=MODEL_PRICING.get(model_id),
        )

    on_stage("starting smoke turn")
    try:
        async with asyncio.timeout(timeout_s):
            tail = await run_turn(
                anthropic=anthropic,
                agent_id=resolved_agent_id,
                environment_id=resolved_environment_id,
                trigger_message=SMOKE_PROMPT,
                usage_record_factory=usage_record_factory,
                session_factory=session_factory,
                tenant_id=tenant_id,
                model_override=SMOKE_MODEL,
            )
    except TimeoutError as err:
        raise SmokeCheckError(f"smoke turn timed out after {timeout_s}s") from err
    on_stage("smoke turn complete")

    on_stage("asserting usage_events row was written")
    async with session_factory() as s:
        rows = await usage_events.list_for_tenant(s, tenant_id=tenant_id, since=started_at)
    if len(rows) == 0:
        raise SmokeCheckError(
            "smoke turn completed but no usage_events row was written for the "
            f"smoke tenant {tenant_id} since the run began at {started_at}"
        )
    on_stage(f"usage_events assertion passed: {len(rows)} row(s)")

    return SmokeResult(
        tenant_id=tenant_id,
        account_id=account_id,
        agent_id=resolved_agent_id,
        environment_id=resolved_environment_id,
        topped_up=topped_up,
        usage_event_count=len(rows),
        tail=tail,
    )
