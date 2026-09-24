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

The module's second half, ``retire_orphaned_turns``, is boot-time orphan-turn
retirement: on every process start it lays to rest every Slack turn whose
render loop died with the previous container, editing the frozen status card
in place and clearing the turn marker. It shares this module with the
reconcile sweep above only because both are boot-time jobs for the Slack
worker -- the two halves have disjoint dependencies, disjoint tables, and no
call into one another.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime
from pathlib import Path

import aiohttp
import anthropic as _anthropic
import structlog
from anthropic import AsyncAnthropic
from cryptography.fernet import InvalidToken
from daimon.adapters.slack.blockkit import to_interrupted_blocks
from daimon.adapters.slack.interactions import resolve_web_client
from daimon.adapters.slack.runtime import SlackRuntime
from daimon.core.defaults.ma_index import find_agent_by_daimon_tag
from daimon.core.defaults.provisioning import reconcile_tenant_defaults
from daimon.core.defaults.report import compose_failure_reason
from daimon.core.errors import DaimonError
from daimon.core.ma import interrupt_orphaned_session
from daimon.core.scope import DeploymentDefault
from daimon.core.stores.domain import ThreadSessionRow
from daimon.core.stores.tenants import list_tenants_by_platform, set_provision_status
from daimon.core.stores.thread_sessions import (
    clear_active_turn_if_message_id,
    list_orphaned_turns,
)
from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_client import AsyncWebClient
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

log = structlog.get_logger()

_SWEEP_CONCURRENCY = 2

# Slack's notification-fallback requirement (blocks= always ships with text=).
_INTERRUPTED_FALLBACK_TEXT = "This turn was interrupted by a restart."

# Everything a chat.update can raise. Mirrors lifecycle.py's _SLACK_SEND_ERRORS
# exactly -- no error-code branching, the same posture the rest of the Slack
# adapter takes on every send.
_SLACK_UPDATE_ERRORS = (SlackApiError, aiohttp.ClientError, TimeoutError)


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


async def retire_orphaned_turns(runtime: SlackRuntime, *, now: datetime) -> None:
    """Lay to rest every Slack turn whose render loop died with the previous
    process, editing its frozen status card in place and clearing its marker.

    A turn's render loop lives in the process that started it, so a deploy
    mid-turn freezes the status card on "thinking" forever while MA completes
    and bills the answer server-side. The user sees a spinner that never
    stops and has no way to tell it is dead. Marking it honestly is cheap;
    a lameduck drain story that avoids this in the first place is not.

    Builds its own per-tenant AsyncWebClient rather than taking one as a
    dependency: unlike Discord's single deployment-wide bot token, Slack's
    token is per-workspace, decrypted per use via ``resolve_web_client``, and
    never cached. Rows are grouped by tenant so a workspace with
    several wedged threads decrypts once, not once per row.

    An unreachable tenant -- an uninstalled workspace (no token row), a
    Fernet key rotated out from under a stored token, or a tenant absent from
    the platform listing -- still has every one of its rows cleared. So does
    a legacy row with no channel (every orphan already in production before
    this shipped, since the column is new, nullable, and unbackfilled) and a
    row whose card edit fails for any reason (deleted message, archived
    channel, revoked token). Otherwise an unreachable or already-gone card
    would be retried on every single boot forever.

    No once-per-process guard: this function lives in a module of free
    functions with no ``self``, so a guard would need module-level mutable
    state, which ``guideline:architecture``
    rule 3 ("no global state") forbids. It is also structurally unnecessary
    here -- the spawn site in ``__main__.py`` is a straight-line statement
    inside ``main()``, and Socket Mode reconnects are handled entirely inside
    ``SocketModeClient`` (``connect()`` starts ``monitor_current_session``,
    which reconnects on its own tasks); ``main()``'s body never re-executes,
    so this function cannot run twice in one process. If this sweep is ever
    moved onto a reconnect-driven hook (an event handler that genuinely
    re-fires, the way Discord's ``on_ready`` does), the guard becomes
    mandatory again -- a marker set by the process that is currently running
    is a live turn, not an orphan.

    This function also assumes ONE Slack adapter process. With two
    overlapping instances -- a rolling deploy that starts the new process
    before the old one has stopped -- it would retire the sibling's genuinely
    live turns, because a marker set by another live process is
    indistinguishable from one left behind by a crash. The compare-and-clear
    below narrows this but does not remove it: a sibling's turn whose marker
    has not changed since this function's read still matches the predicate.
    Gate on an owner id before scaling this service out.

    ``list_orphaned_turns`` snapshots every row once, then the per-tenant,
    per-row loop is sequential with live ``chat_update`` calls. The sweep can
    take a while when many cards need edits. The entrypoint starts this task
    before ``client.connect()``, and mention/continuation turn admission waits
    for it to finish, so this process cannot register a new active marker in
    the snapshot window. A transient sweep failure is retried with capped
    backoff by ``SlackApp``; admission stays gated until a complete pass
    succeeds.

    The compare-and-clear remains useful if another process updates the row
    after this sweep's snapshot: a marker written since the read survives.
    This does not make overlapping adapter processes safe, as described above;
    the sibling's old marker can still match the predicate.
    """
    async with runtime.sessionmaker() as session:
        orphans = await list_orphaned_turns(session, platform="slack")
    if not orphans:
        return
    log.info("slack.turn.orphans_found", count=len(orphans))

    tenants = await list_tenants_by_platform(runtime.sessionmaker, platform="slack")
    team_id_by_tenant = {t.id: t.external_id for t in tenants}

    by_tenant: dict[uuid.UUID, list[ThreadSessionRow]] = {}
    for row in orphans:
        by_tenant.setdefault(row.tenant_id, []).append(row)

    for tenant_id, rows in by_tenant.items():
        client: AsyncWebClient | None = None
        team_id = team_id_by_tenant.get(tenant_id)
        if team_id is not None:
            try:
                client = await resolve_web_client(runtime, team_id=team_id)
            except (InvalidToken, SQLAlchemyError) as err:
                # Per-tenant supervisor boundary -- mirrors _seed_tenant_defaults:
                # one tenant's bad key must not stop the sweep over its siblings.
                log.warning(
                    "slack.turn.orphan_token_unusable", tenant_id=str(tenant_id), error=str(err)
                )
        if client is None:
            # Uninstalled workspace, or a key we can no longer decrypt with.
            # Not an error -- an uninstalled workspace is a normal outcome
            # (app.py:837-839 treats a missing token row the same way). Still
            # clear below so these rows are not retried forever.
            log.info(
                "slack.turn.orphan_tenant_unreachable", tenant_id=str(tenant_id), rows=len(rows)
            )

        for row in rows:
            message_id = row.active_turn_message_id
            if message_id is None:  # pragma: no cover - list_orphaned_turns filters this out
                # list_orphaned_turns only ever returns rows whose
                # active_turn_message_id IS NOT NULL -- this narrows the Pydantic
                # model's str | None honestly rather than asserting or casting
                # past it. A row with no marker is not an orphan, so skipping it
                # here leaks nothing.
                continue
            channel_id = row.active_turn_channel_id
            if client is not None and channel_id is not None:
                try:
                    await client.chat_update(  # pyright: ignore[reportUnknownMemberType]
                        channel=channel_id,
                        ts=message_id,
                        blocks=to_interrupted_blocks(),
                        text=_INTERRUPTED_FALLBACK_TEXT,
                    )
                    log.info(
                        "slack.turn.orphan_retired",
                        thread_id=row.thread_id,
                        channel_id=row.active_turn_channel_id,
                        status_ts=row.active_turn_message_id,
                        # How long the user stared at a spinner -- after a
                        # crash this is the only surviving trace, since the
                        # turn's own logs died with its container.
                        frozen_for_s=(
                            (now - row.active_turn_started_at).total_seconds()
                            if row.active_turn_started_at is not None
                            else None
                        ),
                    )
                except _SLACK_UPDATE_ERRORS as err:
                    log.warning(
                        "slack.turn.orphan_retire_failed", thread_id=row.thread_id, error=str(err)
                    )
            async with runtime.sessionmaker() as session:
                cleared = await clear_active_turn_if_message_id(
                    session, id=row.id, expected_message_id=message_id
                )
                await session.commit()
            if cleared:
                # The row really was orphaned: stop the turn MA is still
                # running for it, so it stops billing and the next mention's
                # message is not sent into a running session (and ignored).
                # Only a cleared row -- a moved marker belongs to a live turn.
                await interrupt_orphaned_session(runtime.anthropic, session_id=row.ma_session_id)
            else:
                # The marker moved between this sweep's read and this row's
                # clear -- a live process wrote it after the read, so it owns
                # the row now and will clear it on its own terminal path, or
                # crash and be picked up by the next boot's sweep. Not a
                # warning: this is the compare-and-clear working as intended.
                log.info(
                    "slack.turn.orphan_marker_moved",
                    thread_id=row.thread_id,
                    message_id=message_id,
                )
