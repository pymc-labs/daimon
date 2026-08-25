"""Boot-time reconcile sweep for Slack tenants.

Discord tenants self-heal on every bot boot (``bot.py::on_ready``); Slack
tenants were seeded at most once — the OAuth install callback provisions the
DB rows only, and the first mention's resolver miss triggers a one-time
reconcile — and then drifted forever: a ``defaults/`` edit (a prompt rewrite,
a new seeded skill) never reached an existing Slack install. This module is
the reconcile half of Discord's ``on_ready`` ported to the Slack worker:
every registered, unarchived Slack tenant is reconciled against the shipped
defaults on every boot, with bounded concurrency. An in-sync tenant costs
provider reads and zero writes — the reconcile's per-resource fingerprint
gate turns a hash match into a skip.

Deliberately NOT ported from Discord's sweep:

- guild permission checks and command-tree sync — Slack scopes and slash
  commands are fixed by the app manifest, so there is nothing to probe or
  register at boot;
- provisioning installs that arrived while the worker was down — Slack
  installs provision synchronously in the OAuth callback, so the tenant row
  always exists before this sweep can run;
- user-visible install/snag announcements — Slack has no "home channel" to
  post into (the app can only speak where it was invited), and the OAuth
  success page is the install-feedback surface. The sweep is silent; logs
  are the operator surface.
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

import anthropic as _anthropic
import structlog
from anthropic import AsyncAnthropic
from daimon.core.defaults.ma_index import find_agent_by_daimon_tag
from daimon.core.defaults.provisioning import reconcile_tenant_defaults
from daimon.core.defaults.report import compose_failure_reason
from daimon.core.errors import DaimonError
from daimon.core.scope import DeploymentDefault
from daimon.core.stores.tenants import list_tenants_by_platform, set_provision_status
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

log = structlog.get_logger()

_SWEEP_CONCURRENCY = 2


async def run_boot_sweep(
    *,
    anthropic: AsyncAnthropic,
    sessionmaker: async_sessionmaker[AsyncSession],
    defaults_root: Path,
    deployment_default: DeploymentDefault,
    public_url: str | None,
) -> None:
    """Reconcile every registered, unarchived Slack tenant, bounded-concurrent.

    Per-tenant failures are isolated: one tenant's provider or DB error is
    logged and recorded on its row without stopping the rest of the sweep.
    """
    tenants = await list_tenants_by_platform(sessionmaker, platform="slack")
    live = [t for t in tenants if t.archived_at is None]
    if not live:
        log.info("slack.boot_sweep_skipped", reason="no unarchived slack tenants")
        return
    log.info("slack.boot_sweep_started", tenants=len(live))
    sem = asyncio.Semaphore(_SWEEP_CONCURRENCY)

    async def _bounded(tenant_id: uuid.UUID, *, was_ready: bool) -> None:
        async with sem:
            await _seed_tenant_defaults(
                anthropic=anthropic,
                sessionmaker=sessionmaker,
                defaults_root=defaults_root,
                deployment_default=deployment_default,
                public_url=public_url,
                tenant_id=tenant_id,
                was_ready=was_ready,
            )

    await asyncio.gather(*(_bounded(t.id, was_ready=t.provision_status == "ready") for t in live))
    log.info("slack.boot_sweep_complete", tenants=len(live))


async def _seed_tenant_defaults(
    *,
    anthropic: AsyncAnthropic,
    sessionmaker: async_sessionmaker[AsyncSession],
    defaults_root: Path,
    deployment_default: DeploymentDefault,
    public_url: str | None,
    tenant_id: uuid.UUID,
    was_ready: bool,
) -> None:
    """Reconcile one tenant and own its status flip. Mirrors the Discord
    ``_seed_tenant_defaults`` semantics, minus the guild embeds:

    - success (report clean AND the deployment's default agent resolves on
      MA): flip to ``ready`` and clear any stale failure reason;
    - failure while ``was_ready``: the tenant STAYS ready — a transient
      provider failure during a boot sweep must not take a working
      workspace's turns offline; only the reason is recorded;
    - failure while pending/failed: flip to ``failed`` with the reason.

    The roster check exists because a non-failing ``ApplyReport`` alone does
    not guarantee the configured default agent exists — ``config.yaml``'s
    ``agent_name`` can drift from every spec under ``defaults_root/agents/``.
    """
    try:
        report = await reconcile_tenant_defaults(
            anthropic,
            sessionmaker,
            defaults_root,
            tenant_id=tenant_id,
            public_url=public_url,
        )
        seed_ok = not report.is_failure()
        roster_failure_reason: str | None = None
        if seed_ok:
            agent_name = deployment_default.agent_name
            if agent_name is None:
                log.info("slack.boot_sweep_roster_check_skipped", tenant_id=str(tenant_id))
            else:
                default_agent = await find_agent_by_daimon_tag(
                    anthropic, tenant_id=tenant_id, name=agent_name
                )
                if default_agent is None:
                    seed_ok = False
                    roster_failure_reason = (
                        f"agent {agent_name!r}: default agent missing from roster after reconcile"
                    )
                    log.warning(
                        "slack.boot_sweep_default_agent_missing",
                        tenant_id=str(tenant_id),
                        agent_name=agent_name,
                    )
        if seed_ok:
            await set_provision_status(
                sessionmaker, tenant_id=tenant_id, status="ready", clear_reason=True
            )
            log.info("slack.boot_sweep_tenant_ready", tenant_id=str(tenant_id))
        else:
            reason = roster_failure_reason or compose_failure_reason(report)
            await _record_failure(
                sessionmaker, tenant_id=tenant_id, reason=reason, was_ready=was_ready
            )
    except (DaimonError, _anthropic.APIError, SQLAlchemyError) as exc:
        # Per-tenant supervisor boundary: one tenant's provider or DB error
        # must not stop the sweep over its siblings.
        log.warning("slack.boot_sweep_tenant_failed", tenant_id=str(tenant_id), error=str(exc))
        await _record_failure(
            sessionmaker,
            tenant_id=tenant_id,
            reason=f"{type(exc).__name__}: {exc}",
            was_ready=was_ready,
        )


async def _record_failure(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    reason: str | None,
    was_ready: bool,
) -> None:
    """Best-effort failure flip. A ready tenant keeps its status (reason only);
    a pending/failed one flips to ``failed``. A DB hiccup during the flip is
    swallowed so the sweep continues — the next boot's sweep is the backstop."""
    try:
        if was_ready:
            await set_provision_status(sessionmaker, tenant_id=tenant_id, reason=reason)
            log.warning(
                "slack.boot_sweep_reconcile_failed_ready_tenant",
                tenant_id=str(tenant_id),
                reason=reason,
            )
        else:
            await set_provision_status(
                sessionmaker, tenant_id=tenant_id, status="failed", reason=reason
            )
    except SQLAlchemyError:
        log.exception("slack.boot_sweep_status_flip_failed", tenant_id=str(tenant_id))
