"""Resume a one-use chat grant after the requester verifies a GitHub link."""

from __future__ import annotations

import asyncio
import uuid
from typing import Literal

import discord
import httpx
import structlog
from anthropic.types.beta import BetaManagedAgentsAgent
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.oauth_github import (
    _ADMIN_RETRY,  # pyright: ignore[reportPrivateUsage]
    _attest_native_admin,  # pyright: ignore[reportPrivateUsage]
)
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools.discord._client import (
    _require_bot_token,  # pyright: ignore[reportPrivateUsage]
    rest_client,
)
from daimon.adapters.mcp.tools.github_linked_grant import (
    CHANGED_MESSAGE,
    GITHUB_DOWN_MESSAGE,
    ask_admin_message,
    linked_grant,
)
from daimon.adapters.mcp.tools.slack._client import slack_web_client
from daimon.core.defaults.ma_index import find_agent_by_daimon_tag
from daimon.core.defaults.metadata import MA_METADATA_KEY_MANAGED, MA_METADATA_KEY_NAME
from daimon.core.github_panel import requester_manages_agent
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.native_admins import AdminAttestationUnavailable
from daimon.core.stores.accounts import get_account, get_account_with_tenant
from daimon.core.stores.domain import Role
from daimon.core.stores.github_access import is_concurrent_grant_change
from daimon.core.stores.github_linked_pending import (
    DB_STATEMENT_DEADLINE,
    FAILURE_DELIVERY_DEADLINE,
    PER_REQUEST_DEADLINE,
    ClaimLostError,
    PendingGrant,
    bound_transaction,
    renew_claim,
    resolve,
    save,
    take,
)
from daimon.core.stores.github_personal_links import mint_link
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

_log = structlog.get_logger(__name__)

AGENT_CHANGED_MESSAGE = "Agent access changed while you were linking.\nAsk again in chat."
FAILED_MESSAGE = "Something went wrong.\nAsk again."
# The browser page shows "Ask again in chat" when every resumed request ended this way.
REQUEST_CHANGED_MESSAGES = frozenset({AGENT_CHANGED_MESSAGE, CHANGED_MESSAGE})


async def save_pending(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
    agent_id: uuid.UUID,
    ma_agent_id: str,
    agent_name: str,
    repo_name: str,
    requested_access: Literal["read", "write"] = "read",
    platform: str,
    platform_user_id: str,
    workspace_id: str,
    parent_channel_id: str,
    thread_id: str,
) -> None:
    await save(
        session,
        tenant_id=tenant_id,
        account_id=account_id,
        agent_id=agent_id,
        ma_agent_id=ma_agent_id,
        agent_name=agent_name,
        repo_name=repo_name,
        requested_access=requested_access,
        platform=platform,
        platform_user_id=platform_user_id,
        workspace_id=workspace_id,
        parent_channel_id=parent_channel_id,
        thread_id=thread_id,
    )


async def _take_pending(
    runtime: McpRuntime,
    *,
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
    platform: str,
    platform_user_id: str,
) -> list[PendingGrant]:
    async with asyncio.timeout(DB_STATEMENT_DEADLINE.total_seconds()):
        async with runtime.session_factory.begin() as session:
            await bound_transaction(session)
            return await take(
                session,
                tenant_id=tenant_id,
                account_id=account_id,
                platform=platform,
                platform_user_id=platform_user_id,
            )


async def _renew_claim(runtime: McpRuntime, claim_token: uuid.UUID | None) -> set[uuid.UUID]:
    """Extend the lease of every request this callback still holds; return their ids.

    Bounded by `DB_STATEMENT_DEADLINE`: a stalled database raises instead of
    stopping the heartbeat silently.
    """
    async with asyncio.timeout(DB_STATEMENT_DEADLINE.total_seconds()):
        async with runtime.session_factory.begin() as session:
            await bound_transaction(session)
            return await renew_claim(session, claim_token)


async def _resolve_pending(runtime: McpRuntime, row: PendingGrant) -> bool:
    async with asyncio.timeout(DB_STATEMENT_DEADLINE.total_seconds()):
        async with runtime.session_factory.begin() as session:
            await bound_transaction(session)
            return await resolve(session, row.id, row.claim_token)


async def _failure_line(runtime: McpRuntime, row: PendingGrant) -> str:
    """Tell the thread this request failed, best effort and within its own deadline."""
    try:
        async with asyncio.timeout(FAILURE_DELIVERY_DEADLINE.total_seconds()):
            await _deliver_result(runtime, row, FAILED_MESSAGE)
    except Exception:
        _log.warning("github.linked_resume.result_delivery_failed", request_id=str(row.id))
    return FAILED_MESSAGE


async def _deliver_result(runtime: McpRuntime, pending: PendingGrant, message: str) -> None:
    if pending.platform == "discord":
        async with rest_client(_require_bot_token(runtime)) as client:
            channel = await client.fetch_channel(int(pending.thread_id))
            if not isinstance(channel, discord.Thread):
                raise ValueError("The originating GitHub grant thread is unavailable.")
            await channel.send(message, allowed_mentions=discord.AllowedMentions.none())
    elif pending.platform == "slack":
        client = await slack_web_client(runtime, team_id=pending.workspace_id)
        await client.chat_postMessage(  # pyright: ignore[reportUnknownMemberType]
            channel=pending.parent_channel_id,
            thread_ts=pending.thread_id,
            text=message,
        )


async def _deliver_recovery(runtime: McpRuntime, pending: PendingGrant, *, status: str) -> None:
    # Import here because the direct connect path imports save_pending from this module.
    from daimon.adapters.mcp.tools.github_connect import (
        _post_personal_link,  # pyright: ignore[reportPrivateUsage]
    )

    recipient = AuthIdentity(
        account_id=pending.account_id,
        tenant_id=pending.tenant_id,
        role=Role.USER,
        platform=pending.platform,
        external_id=pending.workspace_id,
        platform_user_id=pending.platform_user_id,
    )
    if status == "needs_install":
        url = f"https://github.com/apps/{runtime.settings.github_app.app_slug}/installations/new"
        label = "Open GitHub setup"
    else:
        root = runtime.settings.mcp.app_root_url
        if root is None:
            raise ValueError("GitHub linking is unavailable on this server.")
        async with runtime.session_factory.begin() as session:
            await bound_transaction(session)
            url = await mint_link(
                session,
                tenant_id=pending.tenant_id,
                account_id=pending.account_id,
                platform=pending.platform,
                platform_user_id=pending.platform_user_id,
                root_url=str(root),
            )
            await save_pending(
                session,
                tenant_id=pending.tenant_id,
                account_id=pending.account_id,
                agent_id=pending.agent_id,
                ma_agent_id=pending.ma_agent_id,
                agent_name=pending.agent_name,
                repo_name=pending.repo_name,
                requested_access=pending.requested_access,
                platform=pending.platform,
                platform_user_id=pending.platform_user_id,
                workspace_id=pending.workspace_id,
                parent_channel_id=pending.parent_channel_id,
                thread_id=pending.thread_id,
            )
            # The fresh request and the original's resolution commit together:
            # a failure keeps the original and adds nothing, and a claim another
            # callback took over rolls back the new link as well.
            if not await resolve(session, pending.id, pending.claim_token):
                raise ClaimLostError
        label = "Link GitHub"
    await _post_personal_link(
        runtime,
        recipient,
        url=url,
        channel_id=pending.parent_channel_id,
        thread_id=pending.thread_id,
        label=label,
    )


async def live_admin_refusal(
    runtime: McpRuntime,
    *,
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
    platform: str,
    platform_user_id: str,
    agent_name: str,
    repo_label: str,
    changed_message: str,
) -> tuple[Literal["ask_admin", "changed_try_again"], str] | None:
    """Why a linked grant must not run, or None when the requester is an admin on the platform now.

    The stored role can be stale (the person may have lost admin since, and a
    platform whose admin lookup failed leaves it as it was), so both linked-grant
    paths read it live, as the OAuth confirm does. The requester is read in a
    session that has closed before the platform lookup starts. Returns the
    `ConnectResult` status and the message to show.
    """
    async with runtime.session_factory() as session:
        requester = await get_account_with_tenant(session, account_id=account_id)
    if (
        requester is None
        or requester.tenant_id != tenant_id
        or requester.platform != platform
        or requester.platform_user_id != platform_user_id
    ):
        return "changed_try_again", changed_message
    if runtime.fernet is None:
        return "changed_try_again", _ADMIN_RETRY
    try:
        still_admin = await _attest_native_admin(
            requester=requester,
            settings=runtime.settings,
            sessionmaker=runtime.session_factory,
            fernet=runtime.fernet,
        )
    except AdminAttestationUnavailable:
        return "changed_try_again", _ADMIN_RETRY
    if not still_admin:
        return "ask_admin", ask_admin_message(agent_name, repo_label)
    return None


async def _outcome_message(
    runtime: McpRuntime,
    row: PendingGrant,
    *,
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
    platform: str,
    platform_user_id: str,
) -> str:
    """The thread message for one request: runs the grant when the agent is unchanged."""
    agent = await find_agent_by_daimon_tag(runtime.client, tenant_id=tenant_id, name=row.agent_name)
    if (
        agent is None
        or str(agent.id) != row.ma_agent_id
        or derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=str(agent.id)) != row.agent_id
    ):
        return AGENT_CHANGED_MESSAGE

    async def manages_now(
        session: AsyncSession,
        *,
        agent: BetaManagedAgentsAgent = agent,
        row: PendingGrant = row,
    ) -> bool:
        # Runs inside the SERIALIZABLE grant transaction: lock the rows that
        # confer authority so a demotion committed since the snapshot fails it.
        account = await get_account(session, account_id, for_share=True)
        if account is None or account.tenant_id != tenant_id or account.is_external:
            return False
        return await requester_manages_agent(
            session,
            tenant_id=tenant_id,
            account_id=account_id,
            platform=platform,
            platform_user_id=platform_user_id,
            agent_name=agent.name,
            other_names=(
                row.agent_name,
                str(agent.metadata.get(MA_METADATA_KEY_NAME) or ""),
            ),
            ma_agent_id=str(agent.id),
            default=runtime.deployment_default,
            is_daimon_managed=agent.metadata.get(MA_METADATA_KEY_MANAGED) == "true",
            lock_authority=True,
        )

    async with runtime.session_factory() as session:
        allowed = await manages_now(session)
    if not allowed:
        return AGENT_CHANGED_MESSAGE
    refusal = await live_admin_refusal(
        runtime,
        tenant_id=tenant_id,
        account_id=account_id,
        platform=platform,
        platform_user_id=platform_user_id,
        agent_name=agent.name,
        repo_label=row.repo_name,
        changed_message=AGENT_CHANGED_MESSAGE,
    )
    if refusal is not None:
        return refusal[1]
    config = runtime.settings.github_app
    message: str
    try:
        outcome = await linked_grant(
            runtime.session_factory,
            tenant_id=tenant_id,
            account_id=account_id,
            agent_id=row.agent_id,
            agent_name=agent.name,
            repo_name=row.repo_name,
            fernet=runtime.fernet,
            client_id=config.client_id or "",
            client_secret=config.client_secret.get_secret_value() if config.client_secret else "",
            app_id=config.app_id or "",
            private_key=config.private_key.get_secret_value() if config.private_key else "",
            manages_now=manages_now,
            requested_access=row.requested_access,
        )
        message = outcome.message
        if outcome.status in ("needs_install", "needs_link", "relink"):
            try:
                await _deliver_recovery(runtime, row, status=outcome.status)
            except ClaimLostError:
                raise
            except Exception:
                # Private delivery can fail per person; the rest of the
                # batch still runs and the thread says what to do.
                _log.warning(
                    "github.linked_resume.recovery_delivery_failed", request_id=str(row.id)
                )
                message = "I couldn't send the private GitHub link. Ask me again."
    except DBAPIError as exc:
        if not is_concurrent_grant_change(exc) and getattr(exc.orig, "sqlstate", None) != "23505":
            raise
        message = CHANGED_MESSAGE
    except httpx.HTTPError:
        message = GITHUB_DOWN_MESSAGE
    return message


async def _answer_request(
    runtime: McpRuntime,
    row: PendingGrant,
    *,
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
    platform: str,
    platform_user_id: str,
) -> str:
    """Compute one request's outcome and deliver it to its thread (best effort)."""
    try:
        message = await _outcome_message(
            runtime,
            row,
            tenant_id=tenant_id,
            account_id=account_id,
            platform=platform,
            platform_user_id=platform_user_id,
        )
    except ClaimLostError:
        raise
    except Exception:
        _log.exception("github.linked_resume.request_failed", request_id=str(row.id))
        message = FAILED_MESSAGE
    try:
        await _deliver_result(runtime, row, message)
    except Exception:
        _log.warning("github.linked_resume.result_delivery_failed", request_id=str(row.id))
    return message


async def resume_pending(
    runtime: McpRuntime,
    *,
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
    platform: str,
    platform_user_id: str,
) -> list[str]:
    """Run each claimed request on its own and answer it in its thread.

    A request leaves the table only after it has an outcome and its result
    delivery was attempted; recovery is the exception, which deletes the original
    atomically with saving its replacement, before the private delivery. Any
    failure of one request, including after its grant committed, ends that
    request with `FAILED_MESSAGE` and leaves the rest to run. Each request runs
    under `PER_REQUEST_DEADLINE`: one that hits it gets the same failure line and
    the next request still runs. Before each request the lease of every row this
    callback still holds is renewed. The loop's own transactions (take, renew,
    resolve) carry both `asyncio.timeout` and `bound_transaction`, each
    `DB_STATEMENT_DEADLINE`; transactions inside a request are bounded by the
    request deadline. Together with the cleanup of a cancelled request
    (`DETACHED_REFRESH_SETTLE`) and its failure line, that bounds the gap
    between renewals at `PER_REQUEST_DEADLINE + DETACHED_REFRESH_SETTLE +
    FAILURE_DELIVERY_DEADLINE + 2 * DB_STATEMENT_DEADLINE` = 145 s of enforced
    timeouts, plus cancel cleanup no timeout caps (`CANCEL_CLEANUP_ALLOWANCE`; see
    `take`). A heartbeat the database does not answer gets the failure line
    in the thread of the current request and of every request behind it, each
    under its own `FAILURE_DELIVERY_DEADLINE`, and stops the batch. None of
    those rows is resolved: they stay claimed and are retaken after the lease,
    and the thread already says to ask again, so a later cleanup of them
    loses nothing silently. A crash leaves unresolved requests claimed until
    their lease passes. A request whose claim another callback took over is
    skipped.
    """
    pending = await _take_pending(
        runtime,
        tenant_id=tenant_id,
        account_id=account_id,
        platform=platform,
        platform_user_id=platform_user_id,
    )
    results: list[str] = []
    for index, row in enumerate(pending):
        # Heartbeat the whole claim: a slow earlier request must not let the
        # lease of the rows behind it lapse, and a row another callback took
        # over is missing from the held set, so this request is not answered.
        try:
            held = await _renew_claim(runtime, row.claim_token)
        except Exception:
            # The database did not answer in time (or at all). Every request
            # still held, this one and the ones behind it, gets the failure
            # line and nothing is resolved (the database is not answering):
            # a request left silent could be cleaned up later while the
            # browser page reports success. Then the batch stops: going on
            # without renewals would let the leases lapse toward cleanup. The
            # rows stay claimed and are retaken after the lease, as after a
            # crash.
            _log.warning("github.linked_resume.renew_failed", request_id=str(row.id))
            for failed in pending[index:]:
                results.append(await _failure_line(runtime, failed))
            break
        if row.id not in held:
            _log.warning("github.linked_resume.claim_lost", request_id=str(row.id))
            continue
        try:
            async with asyncio.timeout(PER_REQUEST_DEADLINE.total_seconds()):
                message = await _answer_request(
                    runtime,
                    row,
                    tenant_id=tenant_id,
                    account_id=account_id,
                    platform=platform,
                    platform_user_id=platform_user_id,
                )
        except ClaimLostError:
            # Another callback took the request while this one ran; it answers.
            _log.warning("github.linked_resume.claim_lost", request_id=str(row.id))
            continue
        except TimeoutError:
            _log.warning("github.linked_resume.request_timed_out", request_id=str(row.id))
            message = await _failure_line(runtime, row)
        try:
            if not await _resolve_pending(runtime, row):
                _log.info("github.linked_resume.already_resolved", request_id=str(row.id))
        except Exception:
            # The lease expires and the request runs again, which is idempotent.
            _log.warning("github.linked_resume.resolve_failed", request_id=str(row.id))
        results.append(message)
    return results
