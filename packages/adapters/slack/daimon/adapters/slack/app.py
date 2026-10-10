"""SlackApp Socket Mode listener skeleton.

Ack-first entry point with dedup gate, per-event token resolution,
Slack Connect cross-tenant rejection, uninstall teardown routing, and
SIGTERM drain.  Turn orchestration is delegated here.
fills in ``_orchestrate``.

Error boundary: ``_handle_app_mention`` is the named listener boundary.
Core helpers (stores, crypto) carry no try/except — they propagate to here.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import json
import time
import uuid
from collections.abc import Coroutine
from datetime import UTC, datetime
from typing import Any, Literal, cast

import aiohttp
import anthropic
import structlog
from cryptography.fernet import InvalidToken
from daimon.adapters.slack.admin import resolve_admin_status
from daimon.adapters.slack.agent_setup.actions import (
    handle_agent_setup_action,
    handle_agent_setup_command,
)
from daimon.adapters.slack.agent_setup.add_skill import (
    AddSkillDecision,
    evaluate_add_skill_submission,
    run_add_skill_submission,
)
from daimon.adapters.slack.agent_setup.avatar import evaluate_avatar_submission
from daimon.adapters.slack.agent_setup.channel_admins import (
    ChannelAdminsSubmission,
    evaluate_channel_admins_submission,
    run_channel_admins_submission,
)
from daimon.adapters.slack.agent_setup.channel_skills import (
    ChannelSkillsSubmission,
    evaluate_channel_skills_submission,
    run_channel_skills_submission,
)
from daimon.adapters.slack.agent_setup.operator_tokens import (
    OperatorTokenSubmission,
    evaluate_operator_token_submission,
    run_operator_token_submission,
)
from daimon.adapters.slack.agent_setup.panel_views import (
    CALLBACK_ADD_SKILL,
    CALLBACK_AVATAR_UPLOAD,
    CALLBACK_CHANNEL_ADMINS,
    CALLBACK_CHANNEL_SKILLS,
    CALLBACK_OPERATOR_MINT,
)
from daimon.adapters.slack.agent_setup.state import PanelMetadata, decode_private_metadata
from daimon.adapters.slack.agent_setup.submit import (
    evaluate_new_agent_submission,
    run_new_agent_submission,
)
from daimon.adapters.slack.attachments import (
    ProxyUrlContext,
    build_attachment_url_prefix,
    build_image_url_prefix,
    build_skipped_image_prefix,
)
from daimon.adapters.slack.billing_panel.actions import (
    handle_billing_command,
    handle_panel_action,
    handle_topup_select,
)
from daimon.adapters.slack.billing_panel.redeem import (
    REDEEM_CALLBACK_ID,
    RedeemDecision,
    evaluate_redeem_submission,
    handle_redeem_open,
    run_redeem_submission,
)
from daimon.adapters.slack.billing_panel.views import PANEL_ACTION_IDS, REDEEM_OPEN_ACTION_ID
from daimon.adapters.slack.boot_sweep import (
    recover_slack_card_intents,
    retire_orphaned_turns,
    snapshot_slack_card_intents,
)
from daimon.adapters.slack.budget_notice import with_budget_notifier
from daimon.adapters.slack.channel_admin_groups import user_group_ids
from daimon.adapters.slack.channel_reads import load_channel_read_policy
from daimon.adapters.slack.context import (
    build_channel_context_xml,
    build_context_xml,
    build_delta_xml,
)
from daimon.adapters.slack.continuation_dispatch import dispatch_pending_continuations
from daimon.adapters.slack.credential_requests import (
    CredentialSubmissionDecision,
    evaluate_credential_submission,
    handle_credential_request_click,
    run_env_credential_submission,
    run_env_file_credential_submission,
    run_mcp_credential_submission,
    run_repo_bind_credential_submission,
    run_skill_repo_credential_submission,
)
from daimon.adapters.slack.direct_messages import (
    handle_direct_message,
    handle_dm_command,
    is_direct_message_event,
)
from daimon.adapters.slack.errors import (
    NOT_SET_UP_NOTICE,
    SETUP_OUT_OF_DATE_NOTICE,
    generate_request_id,
    render_error_payload,
)
from daimon.adapters.slack.feedback import (
    FEEDBACK_DETAILS_ACTION_ID,
    FeedbackTextDecision,
    evaluate_feedback_text_submission,
    handle_feedback_details_click,
    handle_feedback_vote,
    run_feedback_text_submission,
)
from daimon.adapters.slack.gating import (
    is_external_interactive,
    is_slack_connect_external,
    mentions_bot,
)
from daimon.adapters.slack.github_connect import (
    ACTION_CANCEL,
    ACTION_UPDATE,
    handle_github_cancel_click,
    handle_github_command,
    handle_github_update_click,
)
from daimon.adapters.slack.help import handle_help_command
from daimon.adapters.slack.here import handle_here_command
from daimon.adapters.slack.interactions import build_retry_handlers, resolve_web_client
from daimon.adapters.slack.lifecycle import SlackTurnLifecycle
from daimon.adapters.slack.memory import handle_memory_command
from daimon.adapters.slack.mrkdwn import escape_mrkdwn
from daimon.adapters.slack.names import remember_payload_names
from daimon.adapters.slack.output_delivery import NoticeKeys, deliver_session_outputs
from daimon.adapters.slack.privacy_panel.actions import (
    handle_privacy_block_action,
    handle_privacy_command,
)
from daimon.adapters.slack.privacy_panel.submit import (
    evaluate_delete_submission,
    run_purge_and_update,
)
from daimon.adapters.slack.routine_delivery import make_slack_routine_poster
from daimon.adapters.slack.routines_panel.actions import (
    handle_routine_action,
    handle_routines_command,
)
from daimon.adapters.slack.routines_panel.submit import (
    evaluate_routines_create_submission,
    run_routines_create_submission,
    run_routines_delete_submission,
)
from daimon.adapters.slack.runtime import (
    SlackRuntime,
    admission_refusal_message,
    resolve_bot_display_name,
    responder_account,
    responder_handle,
)
from daimon.adapters.slack.setup_conversations import handle_setup_lifecycle
from daimon.adapters.slack.support_escalation import (
    ASK_HUMAN_ACTION_ID,
    SUPPORT_CALLBACK_ID,
    evaluate_support_submission,
    handle_ask_human_click,
    run_support_submission,
    slack_support_enabled,
)
from daimon.adapters.slack.thread_handoff import (
    HAND_OVER_ACTION_ID,
    build_hand_over_blocks,
    handle_hand_over_click,
)
from daimon.adapters.slack.tool_confirmation import (
    CONFIRMATION_CUSTOM_ID_PREFIX,
    SlackConfirmationCards,
)
from daimon.adapters.slack.vision import (
    SlackFile,
    download_as_image_blocks,
    is_vision_image,
)
from daimon.core.access_policy import DM_SCOPE_PREFIX
from daimon.core.agent_identity import (
    AgentIdentity,
    identity_enabled_for,
    is_builtin_agent,
    resolve_agent_identity,
)
from daimon.core.continuity.continuation import check_wake_responder, load_asking_agent_id
from daimon.core.continuity.messages import (
    render_current_work_must_finish,
    render_preparation_failed,
    render_replacement_summary,
    render_responder_changed_without_handoff,
    render_unexpected_loss,
)
from daimon.core.continuity.wakes import WakeThread, run_wake_poller
from daimon.core.credential_requests import SLACK_ACTION_ID as SLACK_CREDENTIAL_ACTION_ID
from daimon.core.defaults.provisioning import teardown_slack_install
from daimon.core.errors import DaimonError, UserFacingError
from daimon.core.github_connect_delivery import run_connect_notice_poller
from daimon.core.github_credentials import build_multifernet, decrypt_token
from daimon.core.github_request_expiry import run_request_expiry_poller
from daimon.core.ma_identity import derive_agent_uuid, derive_tenant_uuid
from daimon.core.ma_resolver import MAResolverMissError
from daimon.core.observability import capture_exception_with_scope
from daimon.core.routine_delivery import run_delivery_poller
from daimon.core.slack_oauth import build_slack_connect_url
from daimon.core.stores.credential_requests import peek_credential_request
from daimon.core.stores.domain import Role, TaskContinuationRow
from daimon.core.stores.github_access_requests import AccessRequest
from daimon.core.stores.github_connect_notices import ConnectNotice
from daimon.core.stores.slack_bot_tokens import get_slack_bot_token
from daimon.core.stores.slack_connect_prompts import mark_connect_prompted, was_connect_prompted
from daimon.core.stores.slack_event_dedup import insert_if_new
from daimon.core.stores.slack_turn_contexts import (
    create_slack_turn_context,
    delete_slack_turn_context,
)
from daimon.core.stores.slack_user_tokens import get_slack_user_token
from daimon.core.stores.tenants import get_tenant, get_turn_cap
from daimon.core.stores.thread_agent_bindings import get_binding as get_setup_binding
from daimon.core.stores.thread_sessions import (
    clear_active_turn,
    get_live_thread_session,
    mark_turn_active,
    update_watermark,
)
from daimon.core.stores.turn_card_intents import (
    create_turn_card_intent,
    record_turn_card_message,
    retire_turn_card_intent,
)
from daimon.core.stores.turn_origins import get_active_origin
from daimon.core.turn import turn_deadline
from daimon.core.turn.admission import AdmissionDenied, MissingTurnConfigError, admit
from daimon.core.turn.errors import (
    SessionAgentMismatch,
    SessionBusyError,
    SessionPreparationFailed,
)
from daimon.core.turn.lifecycle import TurnLifecycle
from daimon.core.turn.outcomes import observe_turn, record_refusal
from daimon.core.turn.prepare import bind_session
from daimon.core.turn.protection import protection_state, turn_target_protected
from daimon.core.turn.run import run_prepared_turn
from daimon.core.turn.slots import (
    QUEUE_TIMED_OUT_TEXT,
    holding,
    release_turn_slot,
    wait_for_slot,
)
from daimon.core.turn.state import ToolUseBlock
from daimon.core.turn.thread_queue import (
    ThreadQueue,
    claim_dispatch,
    dispatch_and_drain,
    group_by_author,
    release_thread,
)
from daimon.core.turn_keys import list_mounted_key_names
from daimon.core.turn_origin import (
    build_handoff_notice,
    holds_current_channel_admin_grant,
    render_turn_origin,
    turn_origin,
)
from daimon.core.turn_queue import TurnQueue
from slack_sdk.errors import SlackApiError
from slack_sdk.socket_mode.async_client import AsyncBaseSocketModeClient
from slack_sdk.socket_mode.request import SocketModeRequest
from slack_sdk.socket_mode.response import SocketModeResponse
from slack_sdk.web.async_client import AsyncWebClient
from sqlalchemy.exc import SQLAlchemyError

log = structlog.get_logger()
TENANT_CAP_NOTICE = "This workspace has too many chats in flight right now — try again in a moment."

# Grace window for graceful shutdown drain. Must be strictly less
# than the deployment's 60s kill timeout to leave headroom for client.close()
# and health-server cleanup after the drain completes.
_DRAIN_GRACE_S: float = 50.0

# Recovery gates turn admission, so retry transient failures with a capped
# delay instead of leaving the process permanently unable to turn.
_ORPHAN_RECOVERY_RETRY_DELAY_S: float = 1.0
_ORPHAN_RECOVERY_MAX_RETRY_DELAY_S: float = 30.0

# Marks a request queued behind the thread's active turn.
_PENDING_REACTION = "hourglass_flowing_sand"

_CANCEL_NOT_AUTHOR = "Only the person who started this turn can cancel it."
_CANCEL_TURN_ENDED = "This turn has already finished — there is nothing left to cancel."


def _log_bg_task_exception(task: asyncio.Task[None]) -> None:
    """Done-callback: surface escaped background-task exceptions immediately
    instead of asyncio's GC-time 'Task exception was never retrieved'."""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        log.error("bg_task_failed", task_name=task.get_name(), exc_info=exc)


def _author_id(event: dict[str, Any]) -> str:
    """The Slack user id that authored an event, or "" when there is none.

    One spelling, used by both the drain partition and
    ``_compose_queued_content`` — deriving the author two different ways is how
    a partition key and a rendered attribution drift apart.
    """
    return str(event.get("user") or "")


def _compose_queued_content(events: list[dict[str, Any]]) -> str:
    """Compose pending mention texts into a single composite user message.

    Single-author: texts joined by blank lines so the model sees them as
    one continuing thought from the same speaker. Multi-author: each prefixed
    with ``[user_id]: `` so the agent can attribute who said what.
    Mirrors Discord's ``_compose_queued_content`` (bot.py:102-114).
    """
    if not events:
        return ""
    user_ids = {_author_id(e) for e in events}
    if len(user_ids) == 1:
        return "\n\n".join(str(e.get("text", "")) for e in events)
    return "\n\n".join(f"[{_author_id(e)}]: {e.get('text', '')}" for e in events)


def _is_top_level(event: dict[str, Any]) -> bool:
    """Whether the event was posted in the channel itself, not in a thread.

    Decided from the event, never from the session: a first turn in an
    existing thread still replays that thread.
    """
    thread_ts = event.get("thread_ts")
    return not thread_ts or thread_ts == event.get("ts")


def _envelope_event_time(payload: dict[str, Any]) -> datetime | None:
    """The Events API envelope's `event_time` (epoch seconds), or None."""
    raw = payload.get("event_time")
    if isinstance(raw, bool) or not isinstance(raw, int | float):
        return None
    return datetime.fromtimestamp(raw, tz=UTC)


def _revokes_bot_token(event: dict[str, Any]) -> bool:
    """Return True if a ``tokens_revoked`` event revoked the workspace bot token.

    Slack sends ``tokens_revoked`` with ``{"tokens": {"oauth": [...], "bot": [...]}}``.
    The ``oauth`` list fires when a single member revokes their own user token —
    daimon triggers exactly that itself, from /privacy → Disconnect. Only a
    non-empty ``bot`` list means the install is actually gone, so only that may
    reach teardown; treating the whole event as an uninstall let one member's
    Disconnect archive the tenant and delete the bot token for everyone.
    """
    tokens: dict[str, Any] = event.get("tokens") or {}
    return bool(tokens.get("bot"))


def _collect_files(events: list[dict[str, Any]]) -> list[SlackFile]:
    """Flatten the ``files`` arrays across events, preserving order.

    Slack file objects arrive as untyped dicts on the event; we narrow to the
    ``SlackFile`` fields the adapter reads. Events without a ``files`` key
    contribute nothing.
    """
    return [cast(SlackFile, f) for event in events for f in event.get("files", [])]


class SlackApp:
    """Socket Mode listener skeleton.

    Owns the ack-first dispatch, pre-turn safety gates, teardown routing,
    and SIGTERM drain.  Turn orchestration is injected via
    ``_orchestrate``.
    """

    def __init__(self, *, runtime: SlackRuntime) -> None:
        self.runtime = with_budget_notifier(runtime)
        # Slack timestamps are unique only within a workspace conversation.
        self._processing: set[tuple[str, str, str]] = set()
        self._pending: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
        # Continuation dispatches skipped because the conversation was processing,
        # keyed by team, channel, and thread ts: re-run when it is released (see
        # `_release_thread`). Last writer wins; a dispatch reads every pending
        # row for the thread, so one entry is enough.
        self._deferred_dispatch: dict[tuple[str, str, str], dict[str, Any]] = {}
        # Per-tenant turn slots, with the queue a turn waits in at the cap.
        self.turn_queue = TurnQueue.from_settings(runtime.settings.turn_queue, platform="slack")
        # Background task references (prevent GC before done-callbacks fire).
        self._bg_tasks: set[asyncio.Task[None]] = set()
        # Mention handlers can be acked and running before they acquire a
        # thread's _processing slot. Drain waits for these too, closing that
        # pre-orchestration gap without changing crash recovery semantics.
        self._mention_tasks: set[asyncio.Task[None]] = set()
        self._mention_acks_pending: int = 0
        # UUID action keys are global; status timestamps need conversation scope.
        self._cancel_registry: dict[str | tuple[str, str, str], tuple[asyncio.Event, str]] = {}
        # Bot user id per workspace, resolved lazily via auth.test. The id is
        # immutable for a given app+workspace, so the cache never invalidates.
        self._bot_user_ids: dict[str, str] = {}
        # Tool-write confirmation cards awaiting a click (in-process, like the
        # cancel registry above).
        self._confirmations = SlackConfirmationCards()
        # Output-delivery notice dedup per thread and warning log dedup per workspace.
        self._delivery_notice_keys = NoticeKeys()
        # Chains output sweeps per MA session so two never overlap.
        self._output_sweeps: dict[str, asyncio.Task[None]] = {}
        # Drain flag — set on SIGTERM; blocks new mention handling.
        self.draining: bool = False
        self._orphan_recovery_task: asyncio.Task[None] | None = None
        self._card_recovery_task: asyncio.Task[None] | None = None

    def _spawn(self, coro: Coroutine[Any, Any, None]) -> asyncio.Task[None]:
        """Fire-and-forget a background task, tracked so it isn't GC'd."""
        task = asyncio.create_task(coro)
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)
        task.add_done_callback(_log_bg_task_exception)
        return task

    def start_orphan_recovery(self) -> asyncio.Task[None]:
        """Start one recovery sweep before Socket Mode accepts turns."""
        if self._orphan_recovery_task is None:
            self._orphan_recovery_task = self._spawn(self._recover_orphaned_turns())
        return self._orphan_recovery_task

    def start_wake_poller(self) -> asyncio.Task[None]:
        """Poll the wake queue for Slack threads with due work until draining."""
        return self._spawn(
            run_wake_poller(
                self.runtime.sessionmaker,
                platform="slack",
                open_thread=self._open_wake_thread,
                should_stop=lambda: self.draining,
            )
        )

    def start_github_request_expiry_poller(self) -> asyncio.Task[None]:
        return self._spawn(
            run_request_expiry_poller(
                self.runtime.sessionmaker,
                platform="slack",
                post=self._post_github_request_expiry,
                should_stop=lambda: self.draining,
            )
        )

    def start_connect_notice_poller(self) -> asyncio.Task[None]:
        return self._spawn(
            run_connect_notice_poller(
                self.runtime.sessionmaker,
                platform="slack",
                deliver=self._send_connect_notice,
                should_stop=lambda: self.draining,
            )
        )

    async def _send_connect_notice(self, notice: ConnectNotice) -> bool:
        async with self.runtime.sessionmaker() as session:
            tenant = await get_tenant(session, notice.tenant_id)
        if tenant is None:
            return True
        client = await resolve_web_client(self.runtime, team_id=tenant.external_id)
        if client is None:
            return False
        try:
            if (
                notice.encrypted_origin_followup is not None
                and notice.origin_followup_expires_at is not None
                and datetime.now(UTC) < notice.origin_followup_expires_at
            ):
                credentials = build_multifernet(
                    tuple(key.get_secret_value() for key in self.runtime.settings.crypto.keys)
                )
                response_url = decrypt_token(credentials, notice.encrypted_origin_followup)
                async with (
                    aiohttp.ClientSession() as followup_client,
                    followup_client.post(
                        response_url,
                        json={"text": notice.text, "response_type": "ephemeral"},
                        timeout=aiohttp.ClientTimeout(total=10),
                    ) as response,
                ):
                    if response.status < 300:
                        body = await response.text()
                        if not body.lstrip().startswith("{"):
                            return True
                        try:
                            parsed = cast(object, json.loads(body))
                            if not isinstance(parsed, dict):
                                return True
                            payload = cast(dict[str, object], parsed)
                            if payload.get("ok", True):
                                return True
                        except ValueError:
                            return True
                    if response.status >= 500:
                        return False
            if notice.origin_parent_channel_id is None:
                return True  # Old invitations have no private origin; never open a DM.
            if notice.origin_thread_id is not None:
                await client.chat_postEphemeral(  # pyright: ignore[reportUnknownMemberType]
                    channel=notice.origin_parent_channel_id,
                    user=notice.requester_platform_user_id,
                    thread_ts=notice.origin_thread_id,
                    text=notice.text,
                )
            else:
                await client.chat_postEphemeral(  # pyright: ignore[reportUnknownMemberType]
                    channel=notice.origin_parent_channel_id,
                    user=notice.requester_platform_user_id,
                    text=notice.text,
                )
            return True
        except aiohttp.ClientError:
            return False
        except SlackApiError as error:
            return cast(str, error.response["error"]) in ("user_not_found", "account_inactive")

    async def _post_github_request_expiry(self, request: AccessRequest) -> bool:
        try:
            async with self.runtime.sessionmaker() as session:
                tenant = await get_tenant(session, request.tenant_id)
            if tenant is None:
                return True
            client = await resolve_web_client(self.runtime, team_id=tenant.external_id)
            if client is None:
                return False
            await client.chat_postMessage(  # pyright: ignore[reportUnknownMemberType]
                channel=request.parent_channel_id,
                thread_ts=request.thread_id,
                text="Stopped waiting for GitHub access. Ask again any time.",
            )
            return True
        except SlackApiError as error:
            return error.response.get("error") in (  # pyright: ignore[reportUnknownMemberType]
                "channel_not_found",
                "is_archived",
            )

    def start_delivery_poller(self) -> asyncio.Task[None]:
        """Post routine results to their destinations (FEAT-085) until draining."""
        return self._spawn(
            run_delivery_poller(
                self.runtime.sessionmaker,
                platform="slack",
                post=make_slack_routine_poster(self.runtime),
                should_stop=lambda: self.draining,
            )
        )

    async def _open_wake_thread(self, wake: WakeThread) -> bool:
        """The wake poller's hook: dispatch a thread's due wakes, spawned.

        Goes through `dispatch_continuations_in_thread`, so a wake takes the
        same per-thread guard and the same admit -> bind -> run path as any
        continuation. A workspace that is archived or has no stored bot token
        returns False, and the poller pushes its rows back rather than
        offering them every poll; they run if the workspace comes back.
        """
        if self.draining:
            return True
        async with self.runtime.sessionmaker() as session:
            tenant = await get_tenant(session, wake.tenant_id)
        if tenant is None or tenant.archived_at is not None:
            return False
        web_client = await resolve_web_client(self.runtime, team_id=tenant.external_id)
        if web_client is None:
            return False
        self._spawn(
            self.dispatch_continuations_in_thread(
                web_client=web_client,
                tenant_id=wake.tenant_id,
                channel=wake.parent_channel_id,
                thread_id=wake.thread_id,
                account_id=wake.requester_account_id,
                team_id=tenant.external_id,
            )
        )
        return True

    async def _recover_orphaned_turns(self) -> None:
        delay_s = _ORPHAN_RECOVERY_RETRY_DELAY_S
        while True:
            try:
                await retire_orphaned_turns(self.runtime, now=datetime.now(UTC))
                intents = await snapshot_slack_card_intents(self.runtime.sessionmaker)
                if intents:
                    self._card_recovery_task = self._spawn(
                        recover_slack_card_intents(self.runtime, intents)
                    )
                return
            except Exception:
                log.exception("slack.turn.orphan_recovery_failed", retry_delay_s=delay_s)
                await asyncio.sleep(delay_s)
                delay_s = min(delay_s * 2, _ORPHAN_RECOVERY_MAX_RETRY_DELAY_S)

    async def _wait_for_orphan_recovery(self) -> None:
        if self._orphan_recovery_task is not None:
            await self._orphan_recovery_task

    def _forget_output_sweep(self, session_id: str, task: asyncio.Task[None]) -> None:
        """Done-callback: drop the chain entry only if it still points at ``task``."""
        if self._output_sweeps.get(session_id) is task:
            del self._output_sweeps[session_id]

    async def _sweep_session_outputs(
        self,
        previous: asyncio.Task[None] | None,
        web_client: AsyncWebClient,
        *,
        session_id: str,
        channel_id: str,
        thread_ts: str,
        team_id: str,
    ) -> None:
        """Detached post-turn output sweep, chained per MA session.

        Awaiting ``previous`` preserves the serial-sweep-per-session invariant
        post-then-delete needs: two overlapping sweeps could both list a file
        before either deletes it, double-posting with no crash anywhere.
        """
        if previous is not None:
            # That task already logged its own failure; only its completion
            # matters here. CancelledError is a BaseException and deliberately
            # NOT suppressed — a SIGTERM cancellation propagates and correctly
            # cancels this chained successor too.
            with contextlib.suppress(Exception):
                await previous
        try:
            await deliver_session_outputs(
                self.runtime.turn_deps.anthropic,
                web_client,
                session_id=session_id,
                channel_id=channel_id,
                thread_ts=thread_ts,
                notice_keys=self._delivery_notice_keys,
                team_id=team_id,
            )
        except Exception as exc:  # named boundary: a sweep failure must never escape
            log.warning(
                "slack.output_delivery.unhandled_error",
                session_id=session_id,
                thread_id=thread_ts,
                error=str(exc)[:300],
            )

    async def on_request(
        self,
        client: AsyncBaseSocketModeClient,
        req: SocketModeRequest,
    ) -> None:
        """Ack-first Socket Mode event handler.

        ``send_socket_mode_response`` MUST be the first awaited line for all
        envelope types EXCEPT ``view_submission``, where the ack carries the
        computed ``response_action`` payload.

        For ``view_submission`` we call the PURE ``evaluate_delete_submission``
        (no I/O) before the single ack, then ack exactly once with the
        computed payload, then spawn the background purge if needed.

        For all other types (events_api, slash_commands, block_actions) the
        unconditional empty ack fires first; all I/O is spawned as background
        tasks.
        """
        # req.payload field annotation is `dict` (SDK normalises all input to dict in __init__).
        # Annotate explicitly as dict[str, Any] — the Unknown parameter is an SDK stub gap.
        payload: dict[str, Any] = req.payload  # pyright: ignore[reportUnknownVariableType, reportUnknownMemberType]
        raw_event: object = payload.get("event")
        event_for_ack: dict[str, Any] = (
            cast(dict[str, Any], raw_event) if isinstance(raw_event, dict) else {}
        )
        is_app_mention = req.type == "events_api" and (
            event_for_ack.get("type") == "app_mention" or is_direct_message_event(event_for_ack)
        )
        received_before_drain = is_app_mention and not self.draining

        # view_submission acks WITH the computed response_action payload (Pattern 2).
        # evaluate_delete_submission is PURE (no I/O), safe to call before the ack.
        # All other envelope types fall through to the unconditional empty ack below.
        if req.type == "interactive" and payload.get("type") == "view_submission":
            view_vs: dict[str, Any] = payload.get("view") or {}
            cb_id: str = str(view_vs.get("callback_id") or "")
            if cb_id == "privacy_delete":
                decision = evaluate_delete_submission(
                    payload, delete_enabled=self.runtime.settings.privacy.delete_enabled
                )
                await (
                    client.send_socket_mode_response(  # ACK WITH PAYLOAD — pure call above, no I/O
                        SocketModeResponse(
                            envelope_id=req.envelope_id,
                            payload=decision.response_payload,
                        )
                    )
                )
                if (
                    decision.proceed
                    and decision.account_id is not None
                    and decision.view_id is not None
                ):
                    _account_id = decision.account_id
                    _view_id = decision.view_id
                    team_info_vs: dict[str, Any] = payload.get("team") or {}
                    _team_id_vs: str = str(team_info_vs.get("id") or "")
                    user_info_vs: dict[str, Any] = payload.get("user") or {}
                    _user_id_vs: str = str(user_info_vs.get("id") or "")
                    _tenant_id_vs = derive_tenant_uuid(platform="slack", workspace_id=_team_id_vs)

                    async def _run_purge() -> None:
                        wc = await resolve_web_client(self.runtime, team_id=_team_id_vs)
                        if wc is not None:
                            await run_purge_and_update(
                                self.runtime,
                                wc,
                                account_id=_account_id,
                                tenant_id=_tenant_id_vs,
                                platform_user_id=_user_id_vs,
                                view_id=_view_id,
                            )

                    self._spawn(_run_purge())
            elif cb_id == "routines__create":
                # Pure evaluate (no I/O) — must run before the single ack.
                _rc_decision = evaluate_routines_create_submission(payload)
                await (
                    client.send_socket_mode_response(  # ACK WITH PAYLOAD — pure call above, no I/O
                        SocketModeResponse(
                            envelope_id=req.envelope_id,
                            payload=_rc_decision.response_payload,
                        )
                    )
                )
                if _rc_decision.proceed:
                    _rc_team_info: dict[str, Any] = payload.get("team") or {}
                    _rc_team_id: str = str(_rc_team_info.get("id") or "")
                    _rc_user_info: dict[str, Any] = payload.get("user") or {}
                    _rc_user_id: str = str(_rc_user_info.get("id") or "")
                    _rc_view_info: dict[str, Any] = payload.get("view") or {}
                    # view_submission payloads carry no top-level "channel" — the
                    # invoking channel lives in the view's private_metadata.
                    _rc_meta = decode_private_metadata(
                        str(_rc_view_info.get("private_metadata") or "")
                    )
                    _rc_channel_id: str = str(_rc_meta.get("channel_id") or "")
                    _rc_extra: dict[str, Any] = _rc_decision.extra

                    async def _run_routines_create_submission(
                        *,
                        _t: str = _rc_team_id,
                        _u: str = _rc_user_id,
                        _c: str = _rc_channel_id,
                        _e: dict[str, Any] = _rc_extra,
                    ) -> None:
                        wc = await resolve_web_client(self.runtime, team_id=_t)
                        if wc is None:
                            return
                        await run_routines_create_submission(
                            self.runtime,
                            wc,
                            team_id=_t,
                            user_id=_u,
                            channel_id=_c,
                            extra=_e,
                        )

                    self._spawn(_run_routines_create_submission())
            elif cb_id == "routines__delete_confirm":
                # No form fields to validate — ack empty to pop the confirm modal
                # back to the panel, then delete + refresh in the background.
                await client.send_socket_mode_response(
                    SocketModeResponse(envelope_id=req.envelope_id)
                )
                _rd_team_info: dict[str, Any] = payload.get("team") or {}
                _rd_team_id: str = str(_rd_team_info.get("id") or "")
                _rd_user_info: dict[str, Any] = payload.get("user") or {}
                _rd_user_id: str = str(_rd_user_info.get("id") or "")
                _rd_view_info: dict[str, Any] = payload.get("view") or {}
                _rd_meta = decode_private_metadata(str(_rd_view_info.get("private_metadata") or ""))
                _rd_channel_id: str = str(_rd_meta.get("channel_id") or "")
                _rd_routine_id: str = str(_rd_meta.get("routine_id") or "")
                _rd_root_view_id: str = str(_rd_meta.get("root_view_id") or "")

                async def _run_routines_delete_submission(
                    *,
                    _t: str = _rd_team_id,
                    _u: str = _rd_user_id,
                    _c: str = _rd_channel_id,
                    _r: str = _rd_routine_id,
                    _v: str = _rd_root_view_id,
                ) -> None:
                    wc = await resolve_web_client(self.runtime, team_id=_t)
                    if wc is None:
                        return
                    await run_routines_delete_submission(
                        self.runtime,
                        wc,
                        team_id=_t,
                        user_id=_u,
                        channel_id=_c,
                        routine_id=_r,
                        root_view_id=_v,
                    )

                self._spawn(_run_routines_delete_submission())
            elif cb_id == "agent_setup__new_agent":
                # Pure evaluate (no I/O) — must run before the single ack.
                _as_decision = evaluate_new_agent_submission(payload)
                await (
                    client.send_socket_mode_response(  # ACK WITH PAYLOAD — pure call above, no I/O
                        SocketModeResponse(
                            envelope_id=req.envelope_id,
                            payload=_as_decision.response_payload,
                        )
                    )
                )
                if _as_decision.proceed and _as_decision.panel_meta is not None:
                    _as_team_info: dict[str, Any] = payload.get("team") or {}
                    _as_team_id: str = str(_as_team_info.get("id") or "")
                    _as_user_info: dict[str, Any] = payload.get("user") or {}
                    _as_user_id: str = str(_as_user_info.get("id") or "")
                    _as_view_info: dict[str, Any] = payload.get("view") or {}
                    _as_view_id: str = str(_as_view_info.get("id") or "")
                    # view_submission payloads carry no top-level "channel" — the
                    # invoking channel lives in the view's metadata, so success
                    # and refusal ephemerals reach a real channel rather than ""
                    # (channel_not_found).
                    _as_panel_meta = _as_decision.panel_meta
                    _as_channel_id: str = _as_panel_meta.channel_id
                    _as_extra: dict[str, Any] = _as_decision.extra

                    async def _run_agent_setup_submission(
                        *,
                        _t: str = _as_team_id,
                        _u: str = _as_user_id,
                        _c: str = _as_channel_id,
                        _v: str = _as_view_id,
                        _e: dict[str, Any] = _as_extra,
                        _pm: PanelMetadata = _as_panel_meta,
                    ) -> None:
                        wc = await resolve_web_client(self.runtime, team_id=_t)
                        if wc is None:
                            return
                        await run_new_agent_submission(
                            self.runtime,
                            wc,
                            team_id=_t,
                            user_id=_u,
                            channel_id=_c,
                            view_id=_v,
                            meta=_pm,
                            name=str(_e.get("name") or ""),
                            purpose=str(_e["purpose"]) if _e.get("purpose") else None,
                            model=str(_e.get("model") or ""),
                        )

                    self._spawn(_run_agent_setup_submission())
            elif cb_id.startswith("credential_request__"):
                # Pure pre-ack evaluation (Pattern 2, as feedback/agent_setup).
                _cred_decision = evaluate_credential_submission(payload)
                await client.send_socket_mode_response(
                    SocketModeResponse(
                        envelope_id=req.envelope_id,
                        payload=_cred_decision.response_payload,
                    )
                )
                if _cred_decision.proceed:
                    cred_team: dict[str, Any] = payload.get("team") or {}
                    cred_user: dict[str, Any] = payload.get("user") or {}

                    async def _run_credential_submission(
                        _d: CredentialSubmissionDecision = _cred_decision,
                        _team: str = str(cred_team.get("id") or ""),
                        _user: str = str(cred_user.get("id") or ""),
                    ) -> None:
                        async def _dispatch_continuations() -> None:
                            """Run whatever the just-saved value unblocked.

                            Addressed from the request row, never from the
                            form: the modal carries routing handles only, and
                            the thread that is waiting is the one the request
                            was minted in, which may not be where the form
                            was submitted from.
                            """
                            async with self.runtime.sessionmaker() as _session:
                                _row = await peek_credential_request(_session, token=_d.token)
                            if _row is None or _row.origin_thread_id is None:
                                return
                            _client = await resolve_web_client(self.runtime, team_id=_team)
                            if _client is None:
                                return
                            await self.dispatch_continuations_in_thread(
                                web_client=_client,
                                tenant_id=_row.tenant_id,
                                channel=_row.parent_channel_id or _row.channel_id,
                                thread_id=_row.origin_thread_id,
                                account_id=_row.account_id,
                                team_id=_team,
                            )

                        common: dict[str, Any] = {
                            "team_id": _team,
                            "user_id": _user,
                            "channel_id": _d.channel_id,
                            "message_ts": _d.message_ts,
                            "token": _d.token,
                            "dispatch_continuations": _dispatch_continuations,
                        }
                        if _d.kind == "env":
                            await run_env_credential_submission(
                                self.runtime, value=_d.value, **common
                            )
                        elif _d.kind == "env_file":
                            await run_env_file_credential_submission(
                                self.runtime, file_id=_d.file_id or "", **common
                            )
                        elif _d.kind == "mcp":
                            await run_mcp_credential_submission(
                                self.runtime, value=_d.value, **common
                            )
                        elif _d.kind == "skill_repo":
                            await run_skill_repo_credential_submission(
                                self.runtime, value=_d.value, **common
                            )
                        elif _d.kind == "repo":
                            await run_repo_bind_credential_submission(
                                self.runtime, value=_d.value, **common
                            )
                        else:
                            log.info("slack.on_request.unknown_credential_kind", kind=_d.kind)

                    self._spawn(_run_credential_submission())
            elif cb_id == REDEEM_CALLBACK_ID:
                # Pure evaluate (no I/O) — must run before the single ack.
                _pr_decision = evaluate_redeem_submission(payload)
                await (
                    client.send_socket_mode_response(  # ACK WITH PAYLOAD — pure call above, no I/O
                        SocketModeResponse(
                            envelope_id=req.envelope_id,
                            payload=_pr_decision.response_payload,
                        )
                    )
                )
                if _pr_decision.proceed:
                    _pr_team_info: dict[str, Any] = payload.get("team") or {}
                    _pr_user_info: dict[str, Any] = payload.get("user") or {}

                    async def _run_redeem(
                        *,
                        _t: str = str(_pr_team_info.get("id") or ""),
                        _u: str = str(_pr_user_info.get("id") or ""),
                        _d: RedeemDecision = _pr_decision,
                    ) -> None:
                        wc = await resolve_web_client(self.runtime, team_id=_t)
                        if wc is not None:
                            await run_redeem_submission(
                                self.runtime, wc, team_id=_t, user_id=_u, decision=_d
                            )

                    self._spawn(_run_redeem())
            elif cb_id == CALLBACK_CHANNEL_ADMINS:
                # Pure evaluate, then an empty ack closes the form over the
                # routing view the background run refreshes.
                _ca = evaluate_channel_admins_submission(payload)
                await client.send_socket_mode_response(
                    SocketModeResponse(envelope_id=req.envelope_id)
                )
                if _ca is not None:
                    _ca_team: dict[str, Any] = payload.get("team") or {}
                    _ca_user: dict[str, Any] = payload.get("user") or {}

                    async def _run_channel_admins(
                        *,
                        _t: str = str(_ca_team.get("id") or ""),
                        _u: str = str(_ca_user.get("id") or ""),
                        _s: ChannelAdminsSubmission = _ca,
                    ) -> None:
                        wc = await resolve_web_client(self.runtime, team_id=_t)
                        if wc is not None:
                            await run_channel_admins_submission(
                                self.runtime, wc, team_id=_t, user_id=_u, submission=_s
                            )

                    self._spawn(_run_channel_admins())
            elif cb_id == CALLBACK_CHANNEL_SKILLS:
                _cs = evaluate_channel_skills_submission(payload)
                await client.send_socket_mode_response(
                    SocketModeResponse(envelope_id=req.envelope_id)
                )
                if _cs is not None:
                    _cs_team: dict[str, Any] = payload.get("team") or {}
                    _cs_user: dict[str, Any] = payload.get("user") or {}

                    async def _run_channel_skills(
                        *,
                        _t: str = str(_cs_team.get("id") or ""),
                        _u: str = str(_cs_user.get("id") or ""),
                        _s: ChannelSkillsSubmission = _cs,
                    ) -> None:
                        wc = await resolve_web_client(self.runtime, team_id=_t)
                        if wc is not None:
                            await run_channel_skills_submission(
                                self.runtime, wc, team_id=_t, user_id=_u, submission=_s
                            )

                    self._spawn(_run_channel_skills())
            elif cb_id == CALLBACK_OPERATOR_MINT:
                # Pure evaluate, then an empty ack closes the form; the token
                # arrives as an ephemeral and the routing view refreshes.
                _ot = evaluate_operator_token_submission(payload)
                await client.send_socket_mode_response(
                    SocketModeResponse(envelope_id=req.envelope_id)
                )
                if _ot is not None:
                    _ot_team: dict[str, Any] = payload.get("team") or {}
                    _ot_user: dict[str, Any] = payload.get("user") or {}

                    async def _run_operator_token(
                        *,
                        _t: str = str(_ot_team.get("id") or ""),
                        _u: str = str(_ot_user.get("id") or ""),
                        _s: OperatorTokenSubmission = _ot,
                    ) -> None:
                        wc = await resolve_web_client(self.runtime, team_id=_t)
                        if wc is not None:
                            await run_operator_token_submission(
                                self.runtime, wc, team_id=_t, user_id=_u, submission=_s
                            )

                    self._spawn(_run_operator_token())
            elif cb_id == CALLBACK_ADD_SKILL:
                # Pure evaluate, off the loop: errors, a fresh preview, or close and add.
                _as = await asyncio.to_thread(evaluate_add_skill_submission, payload)
                await client.send_socket_mode_response(
                    SocketModeResponse(envelope_id=req.envelope_id, payload=_as.response_payload)
                )
                if _as.proceed:
                    _as_team: dict[str, Any] = payload.get("team") or {}
                    _as_user: dict[str, Any] = payload.get("user") or {}

                    async def _run_add_skill(
                        *,
                        _t: str = str(_as_team.get("id") or ""),
                        _u: str = str(_as_user.get("id") or ""),
                        _d: AddSkillDecision = _as,
                    ) -> None:
                        wc = await resolve_web_client(self.runtime, team_id=_t)
                        if wc is not None:
                            await run_add_skill_submission(
                                self.runtime, wc, team_id=_t, user_id=_u, decision=_d
                            )

                    self._spawn(_run_add_skill())
            elif cb_id == CALLBACK_AVATAR_UPLOAD:
                await client.send_socket_mode_response(
                    SocketModeResponse(
                        envelope_id=req.envelope_id,
                        payload=evaluate_avatar_submission(payload),
                    )
                )
            elif cb_id == "feedback_text":
                # Pure evaluate (no I/O) — must run before the single ack.
                _fb_decision = evaluate_feedback_text_submission(payload)
                await (
                    client.send_socket_mode_response(  # ACK WITH PAYLOAD — pure call above, no I/O
                        SocketModeResponse(
                            envelope_id=req.envelope_id,
                            payload=_fb_decision.response_payload,
                        )
                    )
                )
                if _fb_decision.proceed:
                    _fb_team_info: dict[str, Any] = payload.get("team") or {}
                    _fb_user_info: dict[str, Any] = payload.get("user") or {}

                    async def _run_feedback_text(
                        *,
                        _t: str = str(_fb_team_info.get("id") or ""),
                        _u: str = str(_fb_user_info.get("id") or ""),
                        _d: FeedbackTextDecision = _fb_decision,
                    ) -> None:
                        await run_feedback_text_submission(
                            self.runtime, team_id=_t, user_id=_u, decision=_d
                        )

                    self._spawn(_run_feedback_text())
            elif cb_id == SUPPORT_CALLBACK_ID:
                # Pure evaluate (no I/O) — must run before the single ack.
                _sup = evaluate_support_submission(payload)
                await client.send_socket_mode_response(
                    SocketModeResponse(envelope_id=req.envelope_id, payload=_sup.response_payload)
                )
                if _sup.proceed:
                    self._spawn(run_support_submission(self.runtime, _sup))
            else:
                # Unknown view_submission callback_id — log and ack empty (T-82-20).
                log.info("slack.on_request.unknown_view_submission_callback", callback_id=cb_id)
                await client.send_socket_mode_response(
                    SocketModeResponse(envelope_id=req.envelope_id)
                )
            return  # view_submission fully handled

        # ACK FIRST for all non-view_submission envelope types.
        if is_app_mention:
            # Cover the ack-to-task handoff too: a signal can arrive while this
            # await is suspended after Slack has received the response.
            self._mention_acks_pending += 1
        try:
            await client.send_socket_mode_response(  # ACK FIRST — no I/O before this line
                SocketModeResponse(envelope_id=req.envelope_id)
            )
        except BaseException:
            if is_app_mention:
                self._mention_acks_pending -= 1
            raise
        if is_app_mention:
            # No await occurs before the handler task is registered below.
            self._mention_acks_pending -= 1
        # For /billing's top spenders; in the background, never failing the event.
        remember_payload_names(self.runtime.sessionmaker, req.type, payload)

        if req.type == "events_api":
            event = event_for_ack
            team_id_raw = payload.get("team_id") or event.get("team")
            team_id: str = str(team_id_raw) if team_id_raw is not None else ""
            etype: str | None = event.get("type")
            if etype == "app_mention":
                task = self._spawn(
                    self._handle_app_mention(
                        event,
                        team_id=team_id,
                        received_before_drain=received_before_drain,
                    )
                )
                self._mention_tasks.add(task)
                task.add_done_callback(self._mention_tasks.discard)
            elif is_direct_message_event(event) and received_before_drain:
                task = self._spawn(handle_direct_message(self.runtime, event, team_id=team_id))
                self._mention_tasks.add(task)
                task.add_done_callback(self._mention_tasks.discard)
            elif etype in {
                "channel_archive",
                "channel_unarchive",
                "channel_deleted",
                "group_archive",
                "group_unarchive",
                "group_deleted",
            } or (etype == "message" and event.get("subtype") == "message_deleted"):
                self._spawn(handle_setup_lifecycle(self.runtime, event, team_id=team_id))
            elif etype == "app_uninstalled" or (
                etype == "tokens_revoked" and _revokes_bot_token(event)
            ):
                self._spawn(
                    self._handle_teardown(team_id=team_id, event_time=_envelope_event_time(payload))
                )
        elif req.type == "slash_commands":
            # Slash commands arrive as req.type == "slash_commands".
            # Log req.type for unknown commands so the envelope type can be
            # confirmed from staging logs.
            cmd: str = str(payload.get("command") or "")
            if cmd == "/dm":
                self._spawn(handle_dm_command(self.runtime, payload))
            elif cmd == "/help":
                self._spawn(handle_help_command(self.runtime, payload))
            elif cmd == "/here":
                self._spawn(handle_here_command(self.runtime, payload))
            elif cmd == "/routines":
                self._spawn(handle_routines_command(self.runtime, payload))
            elif cmd == "/billing":
                self._spawn(handle_billing_command(self.runtime, payload))
            elif cmd == "/privacy":
                self._spawn(handle_privacy_command(self.runtime, payload))
            elif cmd == "/github" and str(payload.get("text") or "").strip().startswith("connect"):
                self._spawn(handle_github_command(self.runtime, payload))
            elif cmd in ("/agent-setup", "/github"):
                self._spawn(handle_agent_setup_command(self.runtime, payload))
            elif cmd == "/memory":
                self._spawn(handle_memory_command(self.runtime, payload))
            else:
                log.info(
                    "slack.on_request.unknown_command",
                    command=cmd,
                    req_type=req.type,
                )
        elif req.type == "interactive":
            if payload.get("type") == "block_actions":
                # Reject block actions from an external
                # Slack Connect workspace before any handler resolves reads
                # against the host tenant.
                if is_external_interactive(payload):
                    log.info("slack.on_request.external_block_action_rejected")
                    return
                actions: list[dict[str, Any]] = payload.get("actions") or []
                action_id: str = str(actions[0].get("action_id") or "") if actions else ""
                if action_id == "cancel_turn":
                    # Existing cancel path — KEEP unchanged.
                    self._spawn(self._handle_block_action(payload))
                elif (
                    action_id.startswith("routine_action:")
                    or action_id == "routines_refresh"
                    or action_id == "routines_create"
                ):
                    self._spawn(handle_routine_action(self.runtime, payload))
                elif action_id == "billing_topup":
                    self._spawn(handle_topup_select(self.runtime, payload))
                elif action_id == REDEEM_OPEN_ACTION_ID:
                    self._spawn(handle_redeem_open(self.runtime, payload))
                elif action_id in PANEL_ACTION_IDS:
                    self._spawn(handle_panel_action(self.runtime, payload))
                elif action_id in (
                    "privacy_delete_open",
                    "privacy_export",
                    "privacy_slack_disconnect",
                ):
                    self._spawn(handle_privacy_block_action(self.runtime, payload))
                elif action_id.startswith("agent_setup__"):
                    self._spawn(handle_agent_setup_action(self.runtime, payload))
                elif action_id.startswith("github_new_repo__"):
                    from daimon.adapters.slack.agent_setup.github_new_repo import handle_action

                    self._spawn(handle_action(self.runtime, payload))
                elif action_id.startswith("github_link__"):
                    from daimon.adapters.slack.agent_setup.github_link import handle_action

                    self._spawn(handle_action(self.runtime, payload))
                elif action_id.startswith("github_request__"):
                    from daimon.adapters.slack.agent_setup.github_requests import handle_action

                    self._spawn(handle_action(self.runtime, payload))
                elif action_id == SLACK_CREDENTIAL_ACTION_ID:
                    self._spawn(handle_credential_request_click(self.runtime, payload))
                elif action_id == ACTION_UPDATE:
                    self._spawn(handle_github_update_click(self.runtime, payload))
                elif action_id == ACTION_CANCEL:
                    self._spawn(handle_github_cancel_click(self.runtime, payload))
                elif action_id.startswith("feedback_vote:"):
                    self._spawn(handle_feedback_vote(self.runtime, payload))
                elif action_id == FEEDBACK_DETAILS_ACTION_ID:
                    self._spawn(handle_feedback_details_click(self.runtime, payload))
                elif action_id == ASK_HUMAN_ACTION_ID:
                    self._spawn(handle_ask_human_click(self.runtime, payload))
                elif action_id.startswith(CONFIRMATION_CUSTOM_ID_PREFIX):
                    self._spawn(self._confirmations.handle_click(payload))
                elif action_id == HAND_OVER_ACTION_ID:
                    self._spawn(handle_hand_over_click(self.runtime, payload))
        else:
            # Log unrecognised envelope types so the envelope key can be
            # confirmed or corrected from staging logs (T-82-20).
            log.debug("slack.on_request.unrecognised_envelope_type", req_type=req.type)

    @staticmethod
    def _cancel_registry_key(
        key: str, *, team_id: str = "", channel: str = ""
    ) -> str | tuple[str, str, str]:
        # Lifecycle's temporary action key is a UUID; only Slack message ts
        # values need conversation scope. Empty context supports bare lifecycle
        # callbacks used by unit tests.
        return (team_id, channel, key) if team_id and channel and "." in key else key

    def _register_cancel(
        self,
        status_ts: str,
        cancel: asyncio.Event,
        author_id: str,
        *,
        team_id: str = "",
        channel: str = "",
    ) -> None:
        """Register a turn's cancel Event under its action or scoped status key."""
        self._cancel_registry[
            self._cancel_registry_key(status_ts, team_id=team_id, channel=channel)
        ] = (cancel, author_id)

    def _deregister_cancel(self, status_ts: str, *, team_id: str = "", channel: str = "") -> None:
        """Remove a turn's cancel registry entry on turn completion."""
        self._cancel_registry.pop(
            self._cancel_registry_key(status_ts, team_id=team_id, channel=channel), None
        )

    async def _handle_teardown(self, *, team_id: str, event_time: datetime | None = None) -> None:
        """Archive the install: soft-archive the tenant, delete the bot token.

        Reached from app_uninstalled unconditionally, and from tokens_revoked
        only when the event names the bot token (see _revokes_bot_token).

        Soft-archives the tenant and deletes the bot-token row so subsequent
        events see no token and are dropped cleanly. `event_time` is the
        envelope's: a delivery that arrives after the workspace reinstalled
        finds a newer token and tears nothing down.
        """
        await teardown_slack_install(
            self.runtime.sessionmaker,
            team_id=team_id,
            now=datetime.now(UTC),
            event_time=event_time,
        )

    async def _handle_block_action(self, payload: dict[str, Any]) -> None:
        """Author-gated cancel handler for block_actions interactive payloads.

        Looks up the action's per-turn key first, which is registered before
        the initial post response returns; then falls back to status_ts for
        recovered and legacy cards. If the clicker is the turn's original
        author, sets the cancel Event so the driver cancel-race loop picks it
        up. A refused click — non-author, or no matching registry entry — is
        answered with an ephemeral, because a button that does nothing is
        indistinguishable from a dead bot. The missing-entry case is the same
        symptom as a turn orphaned by a deploy, so the notice is what lets the
        user tell the two apart.
        """
        actions: list[dict[str, Any]] = payload.get("actions") or []
        if not actions or actions[0].get("action_id") != "cancel_turn":
            return
        container: dict[str, Any] | None = payload.get("container")
        status_ts: str = (container.get("message_ts") if container is not None else "") or ""
        user_info: dict[str, Any] | None = payload.get("user")
        clicker: str = (user_info.get("id") if user_info is not None else "") or ""
        action_key = str(actions[0].get("value") or "")
        team: dict[str, Any] = payload.get("team") or {}
        channel_info: dict[str, Any] = payload.get("channel") or {}
        team_id = str(team.get("id") or "")
        channel = str(channel_info.get("id") or (container or {}).get("channel_id") or "")
        entry = self._cancel_registry.get(action_key) or self._cancel_registry.get(
            (team_id, channel, status_ts)
        )
        if entry is None:
            await self._refuse_cancel(payload, clicker=clicker, text=_CANCEL_TURN_ENDED)
            return
        cancel, author_id = entry
        if clicker != author_id:
            await self._refuse_cancel(payload, clicker=clicker, text=_CANCEL_NOT_AUTHOR)
            return
        cancel.set()

    async def _refuse_cancel(self, payload: dict[str, Any], *, clicker: str, text: str) -> None:
        """Best-effort ephemeral explaining a refused Cancel click.

        Resolving the client hits the token store; a workspace whose token is
        gone or unreadable has nothing to notify with, so those failures are
        logged and dropped rather than raised into the background task.
        """
        team: dict[str, Any] = payload.get("team") or {}
        team_id = str(team.get("id") or "")
        channel_info: dict[str, Any] = payload.get("channel") or {}
        container: dict[str, Any] = payload.get("container") or {}
        channel = str(channel_info.get("id") or container.get("channel_id") or "")
        message: dict[str, Any] = payload.get("message") or {}
        parent_thread_ts = message.get("thread_ts")
        if not (team_id and channel and clicker):
            return
        try:
            client = await resolve_web_client(self.runtime, team_id=team_id)
            if client is None:
                return
            await client.chat_postEphemeral(  # pyright: ignore[reportUnknownMemberType]  # slack_sdk **kwargs: Unknown
                channel=channel,
                user=clicker,
                text=text,
                **({"thread_ts": parent_thread_ts} if parent_thread_ts else {}),
            )
        except (SlackApiError, InvalidToken, SQLAlchemyError) as exc:
            log.warning("slack.cancel_refusal_notice_failed", team_id=team_id, exc_info=exc)

    async def _handle_app_mention(
        self,
        event: dict[str, Any],
        *,
        team_id: str,
        received_before_drain: bool = False,
    ) -> None:
        """Pre-turn safety gates → orchestration seam (turn body injected).

        The cap is fetched before these gates so no await splits the queue
        check from its thread claim. Gate order (strict):
        1. Draining check (fast path — no I/O).
        1b. MAY POST: the access-policy protection state; anything but
            unprotected returns silently, before any other I/O can fail.
        2. DEDUP: insert_if_new before any other work.
        3. TOKEN RESOLVE: get_slack_bot_token; drop on None.
        4. PER-EVENT CLIENT: decrypt + AsyncWebClient(token=...) — never cached.
        5. EXPLICIT MENTION GATE: drop events whose text lacks <@bot_user_id>;
           runs before the Connect gate so un-mentioned external senders are
           dropped silently rather than sent a rejection ephemeral.
        6. SLACK CONNECT GATE: ephemeral rejection for external-workspace senders.
        7. TENANT RESOLVE: derive_tenant_uuid.
        8. Handoff to _orchestrate.

        The full handler body is wrapped in the listener-boundary catch
        (DaimonError | anthropic.APIError | SlackApiError).  Core helpers
        are try/except-free — exceptions propagate to this boundary.
        """
        if self.draining and not received_before_drain:
            # Mentions that arrive during the drain window are acked by on_request
            # (ack-first is unconditional) but dropped here before the dedup insert.
            # Slack considers them delivered; they will not be redelivered to the
            # replacement instance — this is inherent to ack-first + drain (IN-02).
            return

        await self._wait_for_orphan_recovery()

        channel: str = event.get("channel") or ""
        event_ts: str = event.get("event_ts") or event.get("ts") or ""
        client: AsyncWebClient | None = None
        tenant_id = derive_tenant_uuid(platform="slack", workspace_id=team_id)

        # (0) MAY POST — decided FIRST, before dedup, the token read or any
        # notice, and never raises. A protected channel, or one whose
        # protection can't be established, hears nothing from this event: no
        # rejection, no reply, not even the boundary's error post below.
        post_state = await protection_state(
            self.runtime.sessionmaker,
            tenant_id=tenant_id,
            channel_id=channel,
            thread_id=event.get("thread_ts") or event.get("ts"),
        )
        if not post_state.may_post:
            log.info(
                "turn.skipped.writers_none",
                team_id=team_id,
                channel_id=channel,
                state=post_state.value,
            )
            return

        try:
            # (1) DEDUP — insert_if_new before any other work.
            async with self.runtime.sessionmaker() as s:
                is_new = await insert_if_new(s, team_id=team_id, channel=channel, event_ts=event_ts)
                await s.commit()
            if not is_new:
                log.debug("slack.event_dropped.duplicate", team_id=team_id, event_ts=event_ts)
                return

            # (2) TOKEN RESOLVE — token-existence = tenant liveness.
            async with self.runtime.sessionmaker() as s:
                row = await get_slack_bot_token(s, team_id=team_id)
            if row is None:
                log.error("slack.event_dropped.no_token", team_id=team_id)
                return

            # (3) PER-EVENT CLIENT — decrypt and construct; NEVER cache on self/runtime.
            fernet = build_multifernet(
                tuple(k.get_secret_value() for k in self.runtime.settings.crypto.keys)
            )
            token = decrypt_token(fernet, row.encrypted_token)
            client = AsyncWebClient(  # per-event only
                token=token, retry_handlers=build_retry_handlers()
            )

            # (4) EXPLICIT MENTION GATE — Slack has been observed delivering
            # app_mention events for un-mentioned thread replies; require the
            # <@bot_user_id> token in the text (Discord parity). Runs BEFORE the
            # Slack Connect gate so external senders who never addressed the bot
            # are dropped silently instead of receiving a rejection ephemeral.
            # The bot user id is resolved once per workspace via auth.test and
            # cached. A failed resolution drops the event without the boundary's
            # error reply: whether the event addressed the bot is unknown, so
            # nothing is posted into a thread that may never have mentioned it.
            # Dedup already recorded the event, so a Slack retry will not
            # re-deliver it — accepted for this once-per-process call.
            try:
                bot_user_id = await self._bot_user_id(team_id, client)
            except SlackApiError as exc:
                log.error(
                    "slack.event_dropped.bot_user_id_unresolved",
                    team_id=team_id,
                    channel=channel,
                    event_ts=event_ts,
                    exc_info=exc,
                )
                capture_exception_with_scope(exc)
                return
            if not mentions_bot(event, bot_user_id=bot_user_id):
                log.info(
                    "slack.event_dropped.no_explicit_mention",
                    team_id=team_id,
                    channel=channel,
                    event_ts=event_ts,
                )
                return

            # (5) SLACK CONNECT GATE — reject external-workspace senders.
            if is_slack_connect_external(event, team_id=team_id):
                await client.chat_postEphemeral(  # pyright: ignore[reportUnknownMemberType]  # slack_sdk **kwargs: Unknown
                    channel=channel,
                    user=event.get("user") or "",
                    # Without thread_ts an in-thread mention's rejection lands at
                    # channel root while the sender watches the thread. Only the real
                    # thread_ts — never a ts fallback, which would tuck the notice
                    # into a thread that does not exist yet. None is dropped by the SDK.
                    thread_ts=event.get("thread_ts"),
                    text=(
                        "Sorry, I can only respond to members of this workspace. "
                        "Please ask a workspace member to mention me instead."
                    ),
                )
                return

            # (6) TENANT RESOLVE — derived above, before the may-post check.

            # (7) Orchestration seam — turn body is delegated here.
            await self._orchestrate(
                event,
                team_id=team_id,
                channel=channel,
                event_ts=event_ts,
                web_client=client,
                tenant_id=tenant_id,
            )

        except (
            DaimonError,
            anthropic.APIError,
            SlackApiError,
            InvalidToken,
            SQLAlchemyError,
        ) as exc:
            request_id = generate_request_id()
            log.error(
                "slack.handle_app_mention_failed",
                team_id=team_id,
                channel=channel,
                event_ts=event_ts,
                request_id=request_id,
                exc_info=exc,
            )
            capture_exception_with_scope(exc)
            # Before the per-event client exists there is no token to post
            # with, so the log line is all that can be done. (post_state is
            # always may-post here: anything else returned above.)
            if client is not None and post_state.may_post:
                with contextlib.suppress(SlackApiError):
                    await client.chat_postMessage(  # pyright: ignore[reportUnknownMemberType]  # slack_sdk **kwargs: Unknown
                        channel=channel,
                        thread_ts=event.get("thread_ts") or event_ts,
                        **render_error_payload(exc, request_id=request_id),
                    )

    async def _bot_user_id(self, team_id: str, client: AsyncWebClient) -> str:
        """This app's bot user in `team_id`, resolved via auth.test once per workspace.

        Empty when Slack names no user; that result is not cached, so the next
        event asks again. Raises `SlackApiError` when auth.test fails.
        """
        bot_user_id = self._bot_user_ids.get(team_id)
        if bot_user_id is not None:
            return bot_user_id
        auth_resp = await client.auth_test()  # pyright: ignore[reportUnknownMemberType]  # SDK kwargs
        bot_user_id = str(auth_resp.get("user_id") or "")
        if bot_user_id:
            self._bot_user_ids[team_id] = bot_user_id
        return bot_user_id

    async def _maybe_post_connect_nudge(
        self,
        web_client: AsyncWebClient,
        *,
        team_id: str,
        slack_user_id: str,
        channel: str,
        thread_ts: str,
    ) -> None:
        """Once-ever ephemeral tip: connect your account for user-token reads.

        Raises on Slack/DB failure — the caller wraps in contextlib.suppress so
        the nudge can never fail the turn. Marked prompted only AFTER a
        successful post so a failed post retries on the next mention.
        """
        slack_settings = self.runtime.settings.slack
        app_root_url = self.runtime.settings.mcp.app_root_url
        if slack_settings is None or app_root_url is None or not slack_user_id:
            return
        async with self.runtime.sessionmaker() as s:
            if (
                await get_slack_user_token(s, team_id=team_id, slack_user_id=slack_user_id)
                is not None
            ):
                return
            if await was_connect_prompted(s, team_id=team_id, slack_user_id=slack_user_id):
                return
        connect_url = build_slack_connect_url(
            app_root_url=app_root_url,
            signing_secret=slack_settings.signing_secret.get_secret_value(),
            team_id=team_id,
            slack_user_id=slack_user_id,
            now=time.time(),
        )
        await web_client.chat_postEphemeral(  # pyright: ignore[reportUnknownMemberType]  # slack_sdk **kwargs: Unknown
            channel=channel,
            user=slack_user_id,
            thread_ts=thread_ts,
            text=(
                "👋 Tip: connect your Slack account and "
                f"{escape_mrkdwn(resolve_bot_display_name(self.runtime.settings))} "
                "can read any channel "
                "or DM *you* can see — no invites needed — plus search your messages "
                "(in a DM with "
                f"{escape_mrkdwn(resolve_bot_display_name(self.runtime.settings))}).\n"
                f"Connect: {connect_url}\n"
                "_The link is personal and expires in about an hour. If your "
                "workspace requires admin approval for app permissions, an admin "
                "may need to approve first. Disconnect any time via `/privacy`._"
            ),
        )
        async with self.runtime.sessionmaker() as s:
            await mark_connect_prompted(
                s, team_id=team_id, slack_user_id=slack_user_id, now=datetime.now(tz=UTC)
            )
            await s.commit()

    def _history_page_limit(self) -> int:
        slack_settings = self.runtime.settings.slack
        assert slack_settings is not None, (
            "SlackApp requires slack settings (entrypoint validates at boot)"
        )
        return slack_settings.history_page_limit

    async def _orchestrate(
        self,
        event: dict[str, Any],
        *,
        team_id: str,
        channel: str,
        event_ts: str,
        web_client: AsyncWebClient,
        tenant_id: uuid.UUID,
    ) -> None:
        """Per-thread queue + ⌛ reaction + coalesce drain + per-tenant cap + turn.

        Gate order (strict):
        1. Per-thread queue check: if thread already processing → reactions_add ⌛
           and enqueue. No slot consumed. The drain or the owner's finally
           removes the ⌛ once that request settles.
        2. Per-tenant cap: read-check-increment in one synchronous span (no await
           between read and increment) — mirrors Discord bot.py:516-529.
        3. Turn body via ``_run_thread_turn``.
        4. Drain loop: pending events coalesced into one follow-up turn.
        5. Finally: release thread slot + in-flight slot.
        """
        thread_id: str = event.get("thread_ts") or event.get("ts") or ""
        if not thread_id:
            log.warning("slack.event_dropped.no_ts", team_id=team_id, channel=channel)
            return
        thread_key = (team_id, channel, thread_id)

        # Setup instructions mention this bot inside the thread. Its own echoed
        # app_mention must not run a turn; other bots may still address Daimon.
        if event.get("bot_id"):
            async with self.runtime.sessionmaker() as session:
                setup_root = await get_setup_binding(
                    session,
                    tenant_id=tenant_id,
                    platform="slack",
                    parent_channel_id=channel,
                    thread_id=thread_id,
                )
            if setup_root is not None:
                if str(event.get("ts") or "") == thread_id:
                    return
                if event.get("user") == await self._bot_user_id(team_id, web_client):
                    return

        assert self.runtime.settings.slack is not None, (
            "SlackApp._orchestrate requires slack settings (entrypoint validates at boot)"
        )
        cap = await get_turn_cap(
            self.runtime.sessionmaker,
            tenant_id=tenant_id,
            default=self.runtime.settings.slack.max_concurrent_turns_per_tenant,
        )

        # (1) Per-thread queue check — queued mentions don't consume a slot.
        if thread_key in self._processing:
            # Append before awaiting reactions_add so a Slack API error on the
            # reaction call does not drop the enqueued event (WR-05).
            self._pending.setdefault(thread_key, []).append(event)
            with contextlib.suppress(SlackApiError, aiohttp.ClientError, asyncio.TimeoutError):
                await web_client.reactions_add(  # pyright: ignore[reportUnknownMemberType]  # slack_sdk **kwargs: Unknown
                    channel=channel,
                    timestamp=event_ts,
                    name=_PENDING_REACTION,
                )
            return

        # (2) Per-tenant turn slot. Admission is one synchronous span (no await
        # between the queue check above and the claim). Over the cap the turn
        # queues: it posts the ordinary card and waits after it
        # (`wait_for_slot` in _run_thread_turn). Only a full queue refuses.
        ticket = self.turn_queue.admit(tenant_id, cap=cap, team_id=team_id, channel_id=channel)
        if ticket is None:
            count = self.turn_queue.in_flight(tenant_id)
            # The rejection below is an ephemeral — it appears in no channel
            # history and no API read. The structured log and outcome row are the server-side
            # trace a shed turn leaves; without it a shed mention is
            # indistinguishable from a dropped event.
            record_refusal(
                self.runtime.sessionmaker,
                tenant_id=tenant_id,
                platform="slack",
                channel_id=channel,
                thread_id=thread_id,
            )
            log.info(
                "turn.skipped.concurrency_shed",
                tenant_id=str(tenant_id),
                team_id=team_id,
                channel_id=channel,
                thread_id=thread_id,
                in_flight=count,
                cap=cap,
                reason="queue_full",
            )
            # Even an ephemeral notice stays out of a protected channel. This
            # branch always returns, so awaiting here can't race the queue check.
            if await turn_target_protected(
                self.runtime.sessionmaker,
                tenant_id=tenant_id,
                channel_id=channel,
                thread_id=thread_id,
            ):
                return
            await web_client.chat_postEphemeral(  # pyright: ignore[reportUnknownMemberType]
                channel=channel,
                user=str(event.get("user") or ""),
                # Real thread only — a shed root mention has no thread yet.
                thread_ts=event.get("thread_ts"),
                text=TENANT_CAP_NOTICE,
            )
            return

        # (3) Run turn + (4) drain loop, (5) finally release.
        with holding(ticket):
            self._processing.add(thread_key)
            try:
                # A protected channel hears nothing from the agent: no reply, no
                # acknowledgement, role, refusal or error notice. Checked right after
                # the thread is claimed -- an await before the claim would let a
                # second mention slip past the queue check -- and before anything
                # is posted; the finally releases the claim.
                if await turn_target_protected(
                    self.runtime.sessionmaker,
                    tenant_id=tenant_id,
                    channel_id=channel,
                    thread_id=thread_id,
                ):
                    log.info(
                        "turn.skipped.writers_none",
                        tenant_id=str(tenant_id),
                        team_id=team_id,
                        channel_id=channel,
                        thread_id=thread_id,
                    )
                    return
                # Immediate ack: session cold-start (defaults reconcile + MA session
                # create) can take seconds before the first status message posts.
                # Inside the try/finally so a transport error here still releases
                # the thread-processing flag and tenant in-flight slot.
                with contextlib.suppress(SlackApiError, aiohttp.ClientError, asyncio.TimeoutError):
                    await web_client.reactions_add(  # pyright: ignore[reportUnknownMemberType]  # slack_sdk **kwargs: Unknown
                        channel=channel,
                        timestamp=event_ts,
                        name="eyes",
                    )
                with contextlib.suppress(
                    SlackApiError, SQLAlchemyError, aiohttp.ClientError, asyncio.TimeoutError
                ):
                    await self._maybe_post_connect_nudge(
                        web_client,
                        team_id=team_id,
                        slack_user_id=str(event.get("user") or ""),
                        channel=channel,
                        thread_ts=thread_id,
                    )
                await self._run_thread_turn(
                    event,
                    channel=channel,
                    web_client=web_client,
                    tenant_id=tenant_id,
                    thread_id=thread_id,
                    team_id=team_id,
                    files=_collect_files([event]),
                )
                await self._drain_pending_mentions(
                    channel=channel,
                    web_client=web_client,
                    tenant_id=tenant_id,
                    thread_id=thread_id,
                    team_id=team_id,
                )
            finally:
                self._release_thread(thread_key)
                still_pending = self._pending.pop(thread_key, [])
                await self._notify_undrained_mentions(
                    still_pending, channel=channel, web_client=web_client, thread_id=thread_id
                )

    @property
    def _thread_queue(self) -> ThreadQueue[tuple[str, str, str], dict[str, Any]]:
        return ThreadQueue(self._processing, self._pending)

    async def _drain_pending_mentions(
        self,
        *,
        channel: str,
        web_client: AsyncWebClient,
        tenant_id: uuid.UUID,
        thread_id: str,
        team_id: str,
    ) -> None:
        def author(event: dict[str, Any]) -> str | None:
            if user := _author_id(event):
                return user
            log.warning(
                "slack.drain.skipped_authorless_event", thread_id=thread_id, team_id=team_id
            )
            return None

        # Composed events leave the thread queue before their batch runs, so the
        # owner's cleanup never sees them. Whatever was composed but not run when
        # the drain exits (authorless events, batches abandoned by a cancel) is
        # cleared here.
        unsettled: dict[int, dict[str, Any]] = {}

        def compose(queued: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
            unsettled.update((id(event), event) for event in queued)
            return group_by_author(queued, author)

        async def run(user_events: list[dict[str, Any]]) -> None:
            try:
                await self._run_thread_turn(
                    user_events[0],
                    channel=channel,
                    web_client=web_client,
                    tenant_id=tenant_id,
                    thread_id=thread_id,
                    content_override=_compose_queued_content(user_events),
                    team_id=team_id,
                    # Merge files from ALL of this author's queued events,
                    # in first-seen order — user_events[0] alone would
                    # silently drop files on their later mentions.
                    files=_collect_files(user_events),
                )
            except (
                DaimonError,
                anthropic.APIError,
                SlackApiError,
                InvalidToken,
                SQLAlchemyError,
                aiohttp.ClientError,
                TimeoutError,
            ) as exc:
                log.exception(
                    "slack.drain.turn_failed",
                    thread_id=thread_id,
                    team_id=team_id,
                    exc_info=exc,
                )
                with contextlib.suppress(SlackApiError):
                    await web_client.chat_postMessage(  # pyright: ignore[reportUnknownMemberType]
                        channel=channel,
                        text=("Sorry, something went wrong handling that — please try again."),
                        thread_ts=thread_id,
                    )
            finally:
                for event in user_events:
                    unsettled.pop(id(event), None)
                await self._clear_pending_reactions(
                    user_events, channel=channel, web_client=web_client
                )

        try:
            await self._thread_queue.drain((team_id, channel, thread_id), compose=compose, run=run)
        finally:
            await self._clear_pending_reactions(
                list(unsettled.values()), channel=channel, web_client=web_client
            )

    async def _clear_pending_reactions(
        self,
        events: list[dict[str, Any]],
        *,
        channel: str,
        web_client: AsyncWebClient,
    ) -> None:
        """Remove the ⌛ from requests whose queued work has settled.

        Best-effort: a second removal, or one after a failed add, reports
        ``no_reaction``, and a stale ⌛ is cosmetic, so no error may reach the
        turn that settled these requests.
        """
        for event in events:
            q_channel: str = event.get("channel") or channel
            q_ts: str = event.get("event_ts") or event.get("ts") or ""
            try:
                await web_client.reactions_remove(  # pyright: ignore[reportUnknownMemberType]  # slack_sdk **kwargs: Unknown
                    channel=q_channel,
                    timestamp=q_ts,
                    name=_PENDING_REACTION,
                )
            except (SlackApiError, aiohttp.ClientError, TimeoutError) as exc:
                log.info(
                    "slack.pending_reaction.remove_failed",
                    channel_id=q_channel,
                    message_ts=q_ts,
                    error=str(exc),
                )

    async def _notify_undrained_mentions(
        self,
        events: list[dict[str, Any]],
        *,
        channel: str,
        web_client: AsyncWebClient,
        thread_id: str,
    ) -> None:
        """Apologise to mentions queued (⌛) but never drained because the owner raised.

        Best-effort: posting failures are ignored.
        """
        for queued_event in events:
            q_channel: str = queued_event.get("channel") or channel
            with contextlib.suppress(SlackApiError):
                await web_client.chat_postMessage(  # pyright: ignore[reportUnknownMemberType]
                    channel=q_channel,
                    text="Sorry, something went wrong handling that — please try again.",
                    thread_ts=thread_id,
                )
        await self._clear_pending_reactions(events, channel=channel, web_client=web_client)

    async def _run_thread_turn(
        self,
        event: dict[str, Any],
        *,
        channel: str,
        web_client: AsyncWebClient,
        tenant_id: uuid.UUID,
        thread_id: str,
        team_id: str,
        content_override: str | None = None,
        files: list[SlackFile] | None = None,
    ) -> None:
        try:
            with observe_turn(
                self.runtime.sessionmaker,
                tenant_id=tenant_id,
                platform="slack",
                channel_id=channel,
                thread_id=thread_id,
            ):
                return await self._run_thread_turn_observed(
                    event,
                    channel=channel,
                    web_client=web_client,
                    tenant_id=tenant_id,
                    thread_id=thread_id,
                    team_id=team_id,
                    content_override=content_override,
                    files=files,
                )
        finally:
            # One slot per turn: a drained follow-up re-enters admission
            # instead of keeping the slot (wait_for_slot).
            release_turn_slot()

    async def _run_thread_turn_observed(
        self,
        event: dict[str, Any],
        *,
        channel: str,
        web_client: AsyncWebClient,
        tenant_id: uuid.UUID,
        thread_id: str,
        team_id: str,
        content_override: str | None = None,
        files: list[SlackFile] | None = None,
    ) -> None:
        """Turn body: admission → card+Cancel → bind_session → marker → context → turn → watermark.

        Immediately after admission, the lifecycle is constructed and its
        status card + Cancel registration are posted (``post_initial()``)
        -- before ``bind_session``, since MA ``sessions.create`` plus the
        interstitial history replay and image download below can hold for
        minutes and the user must see something first. Once ``bind_session``
        returns, the turn marker (message ts, channel, start time) is written
        against the mapping row.

        On first mention for a thread: creates a new MA session + ``thread_sessions``
        row, replays thread history via ``build_context_xml`` (one Slack page),
        or for a top-level mention the channel's preceding messages via
        ``build_channel_context_xml``.
        On follow-up mentions: reuses the existing MA session, replays only the
        delta since the watermark via ``build_delta_xml``.

        One ~45-minute ceiling deadline is computed after admission and shared
        by both the ``bind_session`` and ``run_prepared_turn`` calls below.

        Mirrors Discord ``_orchestrate`` (bot.py:831-1098).
        No try/except — errors propagate to the listener boundary in
        ``_handle_app_mention``.
        """
        # The bot account the mention names, resolved before anything is
        # posted. The mention gate has cached it, so this is normally no Slack
        # call.
        account = responder_account(await self._bot_user_id(team_id, web_client))
        # --- Live admin lookup: one users.info per turn, before admit().
        # admin_status distinguishes "not an admin" (False) from "lookup failed"
        # (None) so the role write below can skip on failure rather than
        # demoting a real admin on a transient Slack error. is_admin is the
        # single local Task 2's context builders also consume — a second lookup
        # would violate the one-call-per-turn constraint.
        author_id = str(event.get("user") or "")
        admin_status = await resolve_admin_status(web_client, user_id=author_id)
        if admin_status is None:
            await web_client.chat_postMessage(  # pyright: ignore[reportUnknownMemberType]  # SDK kwargs
                channel=channel,
                thread_ts=thread_id,
                text="I couldn't verify your workspace role. Please retry; no turn was started.",
            )
            return
        is_admin = admin_status

        # --- Stage one: admission (identity, config cascade, missing-config
        # bail, MA resolve/retrieve, balance gate, cap gate) -- D-01 admit(). ---
        try:
            admission = await admit(
                self.runtime.turn_deps,
                tenant_id=tenant_id,
                platform="slack",
                external_user_id=author_id,
                channel_id=channel,
                thread_id=thread_id,
                role=Role.ADMIN if is_admin else Role.USER,
                platform_role_ids=()
                if is_admin
                else sorted(
                    await user_group_ids(
                        self.runtime, web_client, tenant_id=tenant_id, user_id=author_id
                    )
                ),
                now=datetime.now(UTC),
            )
        except MissingTurnConfigError as err:
            log.info(
                "slack.missing_config",
                team_id=team_id,
                channel_id=channel,
                missing=list(err.missing),
            )
            await web_client.chat_postMessage(  # pyright: ignore[reportUnknownMemberType]
                channel=channel,
                thread_ts=thread_id,
                text=NOT_SET_UP_NOTICE,
            )
            return
        except MAResolverMissError as err:
            log.warning(
                "slack.resolver.miss",
                kind=err.kind,
                daimon_tag=err.daimon_tag,
                tenant_id=str(err.tenant_id),
            )
            await web_client.chat_postMessage(  # pyright: ignore[reportUnknownMemberType]
                channel=channel,
                thread_ts=thread_id,
                text=SETUP_OUT_OF_DATE_NOTICE,
            )
            return
        except AdmissionDenied as err:
            if err.reason == "writers_none":
                # Nothing may be posted into a protected channel, a refusal
                # included; the log is the only trace.
                log.info(
                    "turn.skipped.writers_none",
                    tenant_id=str(tenant_id),
                    team_id=team_id,
                    channel_id=channel,
                    thread_id=thread_id,
                )
            elif err.reason == "invoker_not_allowed":
                log.info(
                    "turn.skipped.invoker_not_allowed",
                    tenant_id=str(tenant_id),
                    user_id=str(event.get("user") or ""),
                    team_id=team_id,
                    channel_id=channel,
                )
            elif err.reason == "runs_elsewhere":
                log.info(
                    "turn.skipped.runs_elsewhere",
                    tenant_id=str(tenant_id),
                    team_id=team_id,
                    channel_id=channel,
                    thread_id=thread_id,
                )
            elif err.reason == "own_agents_only":
                log.info(
                    "turn.skipped.own_agents_only",
                    tenant_id=str(tenant_id),
                    team_id=team_id,
                    channel_id=channel,
                    thread_id=thread_id,
                )
            elif err.reason == "balance_depleted":
                log.info(
                    "turn.skipped.over_balance",
                    tenant_id=str(tenant_id),
                    team_id=team_id,
                    channel_id=channel,
                    thread_id=thread_id,
                )
            elif err.reason == "channel_budget_exceeded":
                log.info(
                    "turn.skipped.over_channel_budget",
                    tenant_id=str(tenant_id),
                    team_id=team_id,
                    channel_id=channel,
                    thread_id=thread_id,
                )
            else:
                log.info(
                    "turn.skipped.over_cap",
                    tenant_id=str(tenant_id),
                    user_id=str(event.get("user") or ""),
                    team_id=team_id,
                    channel_id=channel,
                    thread_id=thread_id,
                )
            if err.reason != "writers_none":
                await web_client.chat_postMessage(  # pyright: ignore[reportUnknownMemberType]
                    channel=channel,
                    thread_ts=thread_id,
                    text=admission_refusal_message(err.reason, self.runtime.settings),
                )
            return

        agent = admission.agent
        _lc_agent_name: str = agent.name
        _lc_model_id: str = agent.model.id
        turn_identity: AgentIdentity | None = None
        try:
            async with self.runtime.sessionmaker.begin() as identity_session:
                turn_identity = await resolve_agent_identity(
                    identity_session,
                    tenant_id=tenant_id,
                    agent_name=agent.name,
                    is_builtin=is_builtin_agent(
                        name=agent.name,
                        metadata=agent.metadata,
                        default_agent_name=self.runtime.deployment_default.agent_name,
                    ),
                    public_base_url=self.runtime.settings.mcp.app_root_url,
                    enabled=identity_enabled_for(self.runtime.settings, "slack", team_id),
                    background_sessionmaker=self.runtime.sessionmaker,
                    wait_for_face=True,
                )
        except (anthropic.APIError, SQLAlchemyError) as exc:
            log.warning("slack.agent_identity_lookup_failed", error_type=type(exc).__name__)

        # Commit the intent before Slack can accept the initial card. If the
        # response is lost or this task is cancelled during the request, a
        # restart can still discover the prepared intent.
        async with self.runtime.sessionmaker() as intent_session:
            card_intent = await create_turn_card_intent(
                intent_session,
                tenant_id=tenant_id,
                platform="slack",
                thread_id=thread_id,
                turn_token=uuid.uuid4(),
                channel_id=channel,
            )
            await intent_session.commit()

        # lifecycle_holder tracks whichever SlackTurnLifecycle actually
        # completed the turn -- recovery_lifecycle rebuilds a fresh one against
        # the recreated session, and the watermark write further down must read
        # final_ts off THAT lifecycle, not the pre-recovery one.
        cancel_event = asyncio.Event()
        lifecycle = SlackTurnLifecycle(
            sessionmaker=self.runtime.sessionmaker,
            alert_webhook_url=self.runtime.settings.ops.alert_webhook_url,
            ask_human=slack_support_enabled(self.runtime.settings.support),
            tenant_id=tenant_id,
            budget_channel_id=admission.budget_channel_id,
            render_tables=self.runtime.settings.table_rendering.get(tenant_id, False) is True,
            client=web_client,
            channel=channel,
            thread_ts=thread_id,
            cancel=cancel_event,
            author_id=str(event.get("user") or ""),
            notify_on_completion=self.runtime.settings.completion_pings.get(tenant_id, False)
            is True,
            trigger_ts=str(event.get("ts") or "") or None,
            agent_name=_lc_agent_name,
            model_id=_lc_model_id,
            markup=self.runtime.turn_deps.markup,
            register=functools.partial(self._register_cancel, team_id=team_id, channel=channel),
            deregister=functools.partial(self._deregister_cancel, team_id=team_id, channel=channel),
            register_pending=functools.partial(
                self._register_cancel, team_id=team_id, channel=channel
            ),
            deregister_pending=functools.partial(
                self._deregister_cancel, team_id=team_id, channel=channel
            ),
            intent_id=card_intent.id,
            identity=turn_identity,
            ma_agent_id=str(agent.id),
        )
        lifecycle_holder: list[SlackTurnLifecycle] = [lifecycle]

        # Post the status card and register Cancel BEFORE bind_session --
        # MA sessions.create plus the history replay and image download below
        # can hold for minutes, and the card must exist before all of that,
        # not merely before run_prepared_turn.
        await lifecycle.post_initial()

        # Make response timestamp persistence a gate before session or turn
        # work. A failure here leaves the visible card's prepared intent for
        # restart lookup and must not run an untracked turn.
        try:
            async with self.runtime.sessionmaker() as intent_session:
                recorded = await record_turn_card_message(
                    intent_session,
                    id=card_intent.id,
                    message_id=lifecycle.status_ts or "",
                )
                await intent_session.commit()
            if not recorded:
                raise RuntimeError("Slack initial card intent could not record its message ID")
        except BaseException:
            if lifecycle.status_ts is not None:
                self._deregister_cancel(lifecycle.status_ts, team_id=team_id, channel=channel)
            raise

        # Mapping-row ids the turn marker has been written against, tracked
        # from here rather than built inline in the finally below: unlike
        # Discord (whose try opens after bind_session, so `prepared` is bound
        # on every path that reaches its finally), this outer try opens BEFORE
        # bind_session, so neither `prepared` nor `outcome` is guaranteed bound
        # at the finally. The accumulator is bound on every path instead.
        _marker_mapping_ids: set[uuid.UUID] = set()

        # Outer bookkeeping try/finally -- registry hygiene and marker hygiene
        # only. It renders nothing, posts nothing, collapses nothing, and adds
        # no error handling: `_handle_app_mention`'s boundary catch keeps
        # handling every failure exactly as it does today. It exists because
        # post_initial() now runs before bind_session, so a raise from the
        # bind, the history replay, the image download, or the turn-context
        # write below would otherwise strand a live-looking Cancel registry
        # entry and a turn marker for the life of the process. The stale card
        # left behind by such a failure is deliberately NOT collapsed here --
        # exact Discord parity, and the boundary catch already posts an
        # in-thread failure notice with a request id (rendered through the
        # existing generic error path, no Slack-specific copy). The clear
        # runs on any exception, not just the happy path, and covers BOTH
        # prepared.mapping_id and outcome.mapping_id because recovery moves
        # the turn to a new mapping row and leaves the marker on the old
        # one -- sufficient because
        # _recovery_lifecycle adopts the pre-recovery card, so the marker left
        # on the old mapping row still addresses the card being rendered into.
        # A ceiling breach is not a special case here either:
        # run_prepared_turn already marked the active mapping dead and returns
        # a normal RunOutcome, so it takes this same path. _deregister_cancel
        # is pop(ts, None), so the double call after on_terminal_success's own
        # finally is harmless; the observable effect is that a Cancel click on
        # a card left behind by a bind-phase failure now gets the honest
        # "already finished" ephemeral instead of silently setting a dead
        # Event. A marker-clear failure must not mask the turn's own outcome,
        # which is why each clear below is individually suppressed on
        # SQLAlchemyError -- a missed clear is recovered by the next boot sweep.
        intent_terminal = False
        try:
            # Over the cap the turn waits here, behind its ordinary card: the
            # queue is never shown. Before the ceiling clock starts, so the
            # wait does not eat the turn's budget.
            slot = await wait_for_slot(
                cancel_event, sessionmaker=self.runtime.sessionmaker, tenant_id=tenant_id
            )
            if slot != "started":
                await lifecycle.end_unstarted(
                    stopped=slot == "cancelled",
                    text={
                        "balance_depleted": admission_refusal_message(
                            "balance_depleted", self.runtime.settings
                        ),
                        "queue_full": TENANT_CAP_NOTICE,
                    }.get(slot, QUEUE_TIMED_OUT_TEXT),
                )
                intent_terminal = True
                return

            # One shared ceiling deadline for THIS turn, computed once the clock
            # starts (D-03/D-04): right after admission passes, not before --
            # admit() itself is deliberately outside the ceiling. Passed as the
            # SAME value to both bind_session and run_prepared_turn below rather
            # than letting each default its own `deadline=None` window -- the
            # fail-safe default exists so no caller is ever unbounded, but a
            # caller that makes BOTH calls would otherwise get two independent
            # ~45-minute budgets (bind, then run) instead of one shared one.
            #
            # This budget does NOT cover the interstitial Slack-API work below
            # (build_context_xml / build_delta_xml thread-history replay,
            # download_as_image_blocks) or the create_slack_turn_context write --
            # all of that is adapter-owned I/O, neither bounded nor cancelled by
            # the deadline, and runs to completion regardless. That is deliberate,
            # not an oversight: it means the interstitial CONSUMES the shared
            # budget rather than getting a window of its own, so a slow history
            # replay leaves the pump less than the full ceiling, and an
            # interstitial that outruns the deadline entirely makes
            # run_prepared_turn fail immediately at its first await with a
            # ceiling TurnError. Bounding the interstitial itself is separate,
            # deferred work.
            turn_deadline_at = turn_deadline(now=datetime.now(UTC))

            # --- Stage two: bind_session (find-or-create, mapping write,
            # recorder binding) -- D-01 bind_session(). As on Discord,
            # session_account_id is the admitted caller's account, and threads
            # always pre-exist. ---
            try:
                prepared = await bind_session(
                    self.runtime.turn_deps,
                    admission,
                    tenant_id=tenant_id,
                    platform="slack",
                    external_user_id=str(event.get("user") or ""),
                    thread_id=thread_id,
                    session_account_id=admission.account_id,
                    reuse_existing=True,
                    deadline=turn_deadline_at,
                )
            except SessionPreparationFailed:
                # Nothing was attempted upstream (the PreparedTurn contract):
                # the old workspace is untouched, so this is not `render_error`'s
                # generic path -- the person is told plainly that the change
                # will be retried at their next message and no turn ran.
                explanation = render_preparation_failed(admission.agent.name)
                if lifecycle.status_ts is not None:
                    await web_client.chat_update(  # pyright: ignore[reportUnknownMemberType]  # SDK kwargs
                        channel=channel,
                        ts=lifecycle.status_ts,
                        text=explanation,
                        blocks=[],
                    )
                    intent_terminal = True
                else:
                    await lifecycle.post_notice(explanation)
                return
            except SessionBusyError:
                # Nothing failed and nothing is misconfigured: the previous
                # turn in this thread is simply still running, and the session
                # it is running in belongs to the OUTGOING responder. Making
                # the change around it would answer as one agent inside
                # another agent's workspace, so no turn runs here -- the
                # person is told the in-flight message finishes first and the
                # switch takes effect at their next message.
                busy_text = render_current_work_must_finish(admission.agent.name, handoff=True)
                if lifecycle.status_ts is not None:
                    await web_client.chat_update(  # pyright: ignore[reportUnknownMemberType]  # SDK kwargs
                        channel=channel,
                        ts=lifecycle.status_ts,
                        text=busy_text,
                        blocks=[],
                    )
                    intent_terminal = True
                else:
                    await lifecycle.post_notice(busy_text)
                return
            except SessionAgentMismatch as error:
                # The recorded (previous) responder's display name is a
                # best-effort live lookup -- it is purely cosmetic copy, so a
                # failed/404 retrieve falls back to a generic owner rather than
                # failing the whole explanation.
                owner_name = "the previous agent"
                try:
                    owner_agent = await self.runtime.anthropic.beta.agents.retrieve(
                        error.source_agent_id
                    )
                    owner_name = owner_agent.name
                except anthropic.APIStatusError:
                    pass
                explanation = render_responder_changed_without_handoff(
                    new_responder=admission.agent.name,
                    owner=owner_name,
                    channel=f"<#{channel}>",
                    offer_button=True,
                )
                hand_over = build_hand_over_blocks(
                    text=explanation,
                    agent_id=admission.agent.id,
                    agent_name=admission.agent.name,
                )
                if lifecycle.status_ts is not None:
                    await web_client.chat_update(  # pyright: ignore[reportUnknownMemberType]  # SDK kwargs
                        channel=channel,
                        ts=lifecycle.status_ts,
                        text=explanation,
                        blocks=hand_over,
                    )
                    intent_terminal = True
                else:
                    await lifecycle.post_notice(explanation, blocks=hand_over)
                return
            ma_session_id = prepared.ma_session_id
            watermark = prepared.watermark
            reused = prepared.reused

            # Continuity facts about this bind, for `<turn_controls>` and the
            # pre-answer notices below. None on the ordinary "nothing changed"
            # path so a plain turn's controls are byte-identical to before
            # this existed.
            session_state = (
                prepared.continuity.session_state()
                if prepared.continuity.state != "continued"
                else None
            )
            # `prepared.continuity` is the bind's OWN decision, taken before
            # the turn runs -- it can never be "replaced_after_loss" (that
            # state only exists on the post-turn `RunOutcome`, set when
            # `run_prepared_turn`'s recovery cycle recreates the session
            # mid-call). That notice is posted after the turn instead, once
            # the outcome is known.
            replacement_summary: str | None = None
            if (
                prepared.continuity.announces_replacement()
                and prepared.continuity.transfer_kind is not None
            ):
                # Not a separate message: the answer is an in-place edit of the
                # status card posted at mention time, and Slack orders by the
                # original ts -- so a summary posted at any point after that
                # card reads BELOW the answer it explains ("the file vanished"
                # above "here is why"). It becomes the answer's first paragraph
                # instead. The fallback after the turn covers an answer that
                # never arrives to carry it.
                #
                # A `None` transfer_kind here means a fresh start (no prior
                # session to summarize a transfer from) -- that path already
                # announces itself elsewhere, so no prefix is rendered.
                replacement_summary = render_replacement_summary(
                    prepared.continuity.transfer_kind, lost=[]
                )
                lifecycle.answer_prefix = replacement_summary
            if prepared.continuity.state == "replaced" and prepared.mapping_id is not None:
                from daimon.core.stores.thread_sessions import github_key_restart_line

                async with self.runtime.sessionmaker() as notice_session:
                    key_restart = await github_key_restart_line(
                        notice_session, mapping_id=prepared.mapping_id
                    )
                if key_restart is not None:
                    replacement_summary = key_restart
                    lifecycle.answer_prefix = key_restart

            # Turn marker: message ts + channel + start time, written as soon as
            # the mapping row is known and the card exists. Slack passes
            # active_turn_channel_id where Discord does not -- a Slack message is
            # addressed by (channel, ts), so a boot sweep cannot repair a wedged
            # card without the channel to address the update call.
            if prepared.mapping_id is not None and lifecycle.status_ts is not None:
                async with self.runtime.sessionmaker() as _at_session:
                    await mark_turn_active(
                        _at_session,
                        id=prepared.mapping_id,
                        active_turn_message_id=lifecycle.status_ts,
                        active_turn_channel_id=channel,
                        now=datetime.now(UTC),
                    )
                    await _at_session.commit()
                _marker_mapping_ids.add(prepared.mapping_id)

            log.info(
                "slack.session.ready",
                session_id=ma_session_id,
                thread_id=thread_id,
                reused=reused,
                watermark=watermark,
            )

            # Names-only <keys> context: stored key names for THIS agent, named
            # only while the mounted `.env` is still exactly today's agent_files
            # rows (see `list_mounted_key_names`). One extra read per turn.
            async with self.runtime.sessionmaker() as _keys_session:
                _live_row_for_keys = await get_live_thread_session(
                    _keys_session,
                    tenant_id=tenant_id,
                    platform="slack",
                    thread_id=thread_id,
                    account_id=admission.account_id,
                )
                _live_config = (
                    None if _live_row_for_keys is None else _live_row_for_keys.effective_config
                )
                _env_sha256 = None if _live_config is None else _live_config.env_sha256
                key_names = await list_mounted_key_names(
                    _keys_session,
                    tenant_id=tenant_id,
                    agent_id=derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=str(agent.id)),
                    env_sha256=_env_sha256,
                )

            # --- Build user message ---
            proxy_base = self.runtime.settings.mcp.app_root_url
            proxy_secret = (
                self.runtime.settings.mcp.jwt_secret.get_secret_value()
                if self.runtime.settings.mcp.jwt_secret is not None
                else None
            )
            now_i = int(time.time())
            # None when the proxy is unconfigured — no signing secret to mint URLs with.
            proxy_ctx = (
                ProxyUrlContext(
                    public_url=proxy_base, secret=proxy_secret, team_id=team_id, now=now_i
                )
                if proxy_base is not None and proxy_secret is not None
                else None
            )

            user_text = (
                content_override if content_override is not None else str(event.get("text") or "")
            )

            async def _first_turn_context() -> str:
                """A top-level mention's channel up to the mention, else its thread.

                Rebuilt on a recovery reseed with the same cutoff (the
                trigger) and the access policy as it is then.
                """
                if _is_top_level(event):
                    return await build_channel_context_xml(
                        web_client,
                        channel=channel,
                        trigger_ts=str(event.get("ts") or thread_id),
                        user_query=user_text,
                        read_policy=await load_channel_read_policy(
                            self.runtime.sessionmaker,
                            tenant_id=tenant_id,
                            grant=admission.grant,
                            channel_id=channel,
                            thread_ts=thread_id,
                        ),
                        author_id=author_id,
                        is_admin=is_admin,
                        proxy=proxy_ctx,
                        key_names=key_names,
                    )
                return await build_context_xml(
                    web_client,
                    channel=channel,
                    thread_ts=thread_id,
                    user_query=user_text,
                    author_id=author_id,
                    is_admin=is_admin,
                    proxy=proxy_ctx,
                    key_names=key_names,
                    page_limit=self._history_page_limit(),
                    status_ts=lifecycle.status_ts,
                )

            if not reused:
                user_message = await _first_turn_context()
            elif watermark is not None:
                # Continuation: replay only messages since the last watermark.
                user_message = await build_delta_xml(
                    web_client,
                    channel=channel,
                    thread_ts=thread_id,
                    watermark_ts=watermark,
                    user_query=user_text,
                    author_id=author_id,
                    is_admin=is_admin,
                    proxy=proxy_ctx,
                    key_names=key_names,
                    page_limit=self._history_page_limit(),
                    status_ts=lifecycle.status_ts,
                )
            else:
                # Reused session with no watermark (prior turn's final_ts was None).
                # The MA session already holds prior context; replay only the new query.
                user_message = user_text

            # --- Attachments & vision ---
            files = files or []
            trigger_images = [f for f in files if is_vision_image(f)]
            data_files = [f for f in files if not is_vision_image(f)]

            image_blocks, images_skipped = await download_as_image_blocks(
                trigger_images, token=web_client.token or "", http_client=self.runtime.http_client
            )
            skipped_ids = {f["id"] for f, _ in images_skipped}
            inlined = [f for f in trigger_images if f["id"] not in skipped_ids]

            synthetic_prefix = ""
            if proxy_ctx is not None:
                synthetic_prefix = "\n".join(
                    part
                    for part in (
                        build_attachment_url_prefix(data_files, proxy_ctx),
                        build_image_url_prefix(inlined, proxy_ctx),
                        build_skipped_image_prefix(images_skipped, proxy_ctx),
                    )
                    if part
                )
                if synthetic_prefix:
                    user_message = synthetic_prefix + "\n" + user_message

                # Only claim the images were "linked" when the proxy is configured —
                # that's the branch that actually minted fetchable URLs into the prefix.
                if images_skipped:
                    await lifecycle.post_notice(
                        (
                            "Some images couldn't be inlined — I've linked them for the agent to "
                            "fetch instead: "
                            + ", ".join(f"`{f['name']}` ({r})" for f, r in images_skipped)
                        ),
                    )

            # --- Run the turn (D-08/D-09/D-10: run_prepared_turn owns the driver
            # call and the one-shot dead-session recovery cycle). lifecycle and
            # lifecycle_holder were constructed above, before bind_session. ---
            log.info(
                "slack.turn.started",
                thread_id=thread_id,
                session_id=ma_session_id,
                reused=reused,
            )

            async def _reseed_user_message() -> str:
                """Full history re-seed for the recreated session (dead-session recovery)."""
                full_message = await _first_turn_context()
                if synthetic_prefix:
                    full_message = synthetic_prefix + "\n" + full_message
                async with self.runtime.sessionmaker() as session:
                    recovery_origin = await get_active_origin(
                        session,
                        origin_id=origin.id,
                        tenant_id=tenant_id,
                        account_id=admission.account_id,
                        platform="slack",
                        now=datetime.now(UTC),
                    )
                if recovery_origin is None:
                    raise UserFacingError(
                        "This turn's setup context expired. Please retry your message."
                    )
                return (
                    render_turn_origin(
                        recovery_origin,
                        responder_handle=responder_handle(self.runtime.settings),
                        responder_account=account,
                        session_state=session_state,
                        is_channel_admin=is_channel_admin,
                    )
                    + "\n"
                    + full_message
                )

            def _recovery_lifecycle(cancel: asyncio.Event) -> TurnLifecycle:
                new_lifecycle = SlackTurnLifecycle(
                    sessionmaker=self.runtime.sessionmaker,
                    alert_webhook_url=self.runtime.settings.ops.alert_webhook_url,
                    ask_human=slack_support_enabled(self.runtime.settings.support),
                    tenant_id=tenant_id,
                    budget_channel_id=admission.budget_channel_id,
                    render_tables=self.runtime.settings.table_rendering.get(tenant_id, False)
                    is True,
                    client=web_client,
                    channel=channel,
                    thread_ts=thread_id,
                    cancel=cancel,
                    author_id=str(event.get("user") or ""),
                    notify_on_completion=self.runtime.settings.completion_pings.get(
                        tenant_id, False
                    )
                    is True,
                    trigger_ts=str(event.get("ts") or "") or None,
                    agent_name=_lc_agent_name,
                    model_id=_lc_model_id,
                    markup=self.runtime.turn_deps.markup,
                    register=functools.partial(
                        self._register_cancel, team_id=team_id, channel=channel
                    ),
                    deregister=functools.partial(
                        self._deregister_cancel, team_id=team_id, channel=channel
                    ),
                    register_pending=functools.partial(
                        self._register_cancel, team_id=team_id, channel=channel
                    ),
                    deregister_pending=functools.partial(
                        self._deregister_cancel, team_id=team_id, channel=channel
                    ),
                    # Take over the failed attempt's card so it is edited into
                    # this turn's answer rather than left standing beside a
                    # second, successful card.
                    adopt_status_ts=lifecycle.status_ts,
                    header_customized=(
                        lifecycle.header_customized
                        and identity_enabled_for(self.runtime.settings, "slack", team_id)
                    ),
                    intent_id=card_intent.id,
                    identity=turn_identity,
                    ma_agent_id=str(agent.id),
                )
                # The replacement summary belongs to the turn, not to the
                # lifecycle object that happens to render it -- a recovery
                # cycle swaps the lifecycle and would otherwise drop it.
                new_lifecycle.answer_prefix = lifecycle.answer_prefix
                lifecycle_holder[0] = new_lifecycle
                # An adopting lifecycle never posts, so it never re-registers
                # itself -- the entry left by the original post is still bound
                # to the first attempt's Event, and a click on the adopted card
                # must stop the turn that is actually running. This rebind is a
                # plain dict assignment, so re-registering the same key
                # overwrites in place, and it lands before the recovery turn's
                # first flush, not after it.
                if lifecycle.status_ts is not None:
                    self._register_cancel(
                        lifecycle.status_ts,
                        cancel,
                        str(event.get("user") or ""),
                        team_id=team_id,
                        channel=channel,
                    )
                return new_lifecycle

            async with self.runtime.sessionmaker() as s:
                turn_context = await create_slack_turn_context(
                    s,
                    tenant_id=tenant_id,
                    account_id=admission.account_id,
                    channel_id=channel,
                    thread_ts=thread_id,
                    started_at=datetime.now(tz=UTC),
                )
                await s.commit()
            is_channel_admin = await holds_current_channel_admin_grant(
                self.runtime.sessionmaker,
                tenant_id=tenant_id,
                account_id=admission.account_id,
                platform="slack",
                parent_channel_id=channel,
                role=Role.ADMIN if is_admin else Role.USER,
            )
            try:
                async with turn_origin(
                    self.runtime.sessionmaker,
                    tenant_id=tenant_id,
                    account_id=admission.account_id,
                    platform="slack",
                    parent_channel_id=channel,
                    thread_id=thread_id,
                    responder_ma_agent_id=str(agent.id),
                    is_setup=admission.config.thread_binding_kind == "setup",
                    responder_name=admission.config.agent_name or agent.name,
                    configuration_target_ma_agent_id=admission.config.configuration_target_ma_agent_id,
                    configuration_target_name=admission.config.configuration_target_name,
                    role=Role.ADMIN if is_admin else Role.USER,
                ) as origin:
                    outcome = await run_prepared_turn(
                        self.runtime.turn_deps,
                        prepared,
                        tenant_id=tenant_id,
                        platform="slack",
                        thread_id=thread_id,
                        external_user_id=str(event.get("user") or ""),
                        user_message=(
                            render_turn_origin(
                                origin,
                                responder_handle=responder_handle(self.runtime.settings),
                                responder_account=account,
                                session_state=session_state,
                                is_channel_admin=is_channel_admin,
                            )
                            + "\n"
                            + user_message
                        ),
                        lifecycle=lifecycle,
                        cancel=cancel_event,
                        reseed_user_message=_reseed_user_message,
                        recovery_lifecycle=_recovery_lifecycle,
                        image_blocks=image_blocks or None,
                        render_interval_s=2.0,
                        deadline=turn_deadline_at,
                        confirm_write=self._confirmations.hook(
                            web_client,
                            channel=channel,
                            thread_ts=thread_id,
                            identity=turn_identity,
                            record_post=lifecycle.record_post,
                        ),
                    )
                    intent_terminal = lifecycle_holder[0].final_ts is not None
            finally:
                # Leak-policy bookkeeping only — a delete failure must not mask the
                # turn's own outcome; stale rows age out via the reader-side TTL.
                with contextlib.suppress(SQLAlchemyError):
                    async with self.runtime.sessionmaker() as s:
                        await delete_slack_turn_context(s, id=turn_context.id)
                        await s.commit()

            if outcome.mapping_id is not None:
                _marker_mapping_ids.add(outcome.mapping_id)

            mapping_id = outcome.mapping_id
            final_lifecycle = lifecycle_holder[0]

            # The recovery cycle inside run_prepared_turn only learns a
            # session was lost mid-call, after the turn has already run -- so
            # unlike the planned-replacement summary above, this notice can
            # only be posted here, once `outcome.continuity` is known. Posted
            # regardless of whether the recovered turn itself then answered
            # or errored: the person needs to know the workspace was lost
            # either way.
            if outcome.continuity.state == "replaced_after_loss":
                loss_kind: Literal["transcript", "history"] = (
                    "transcript" if outcome.continuity.transfer_kind == "transcript" else "history"
                )
                loss_notice = render_unexpected_loss(loss_kind)
                # Edited in above the answer rather than posted under it, for
                # the same ordering reason as the planned summary above. Falls
                # back to a message when there is no answer to sit above (a
                # tool-only or failed turn) or it will not fit.
                if not await final_lifecycle.prepend_revealed_answer(loss_notice):
                    await final_lifecycle.post_notice(loss_notice)
            if replacement_summary is not None and not final_lifecycle.answer_prefix_applied:
                # The turn produced no answer to carry the summary (tool-only,
                # cancelled, or failed). The person still has to be told what
                # the replacement carried across, so it goes out on its own.
                await final_lifecycle.post_notice(replacement_summary)

            # --- Watermark --- Preserves Slack's original gate exactly (unconditional
            # on final_ts, no state.error branch) -- Discord's inline sequence had an
            # error-vs-success split here, but that is NOT one of SPEC Req 7's four
            # named behaviour changes, so it is not introduced for Slack either.
            if mapping_id is not None and final_lifecycle.final_ts is not None:
                async with self.runtime.sessionmaker() as s:
                    await update_watermark(
                        s, id=mapping_id, watermark_message_id=final_lifecycle.final_ts
                    )
                    await s.commit()
                log.info(
                    "slack.watermark.updated",
                    thread_id=thread_id,
                    watermark=final_lifecycle.final_ts,
                )
            else:
                log.info(
                    "slack.turn.completed", thread_id=thread_id, session_id=outcome.ma_session_id
                )

            # Deferred-change notice: the bind ran this turn against the
            # session as it stood before the change (an active turn was
            # already running when the change landed), so the person is told
            # the change is saved and will apply at their NEXT message here,
            # not this one -- the answer they just got used the old config.
            if prepared.continuity.pending:
                await final_lifecycle.post_notice(
                    render_current_work_must_finish(admission.agent.name, handoff=False)
                )

            # This turn's own marker is cleared BEFORE anything is dispatched.
            # A continuation's `bind_session` treats a marker on the thread's
            # live row as "a turn is still running", and a responder change
            # that cannot be made yet is deferred onto the session that is
            # running -- which is the OUTGOING agent's. Dispatching first
            # therefore ran the handoff's first turn inside the source agent's
            # workspace while the card credited the destination. The `finally`
            # below still clears every id; `clear_active_turn` is idempotent,
            # and a clear failure there is recovered by the next boot sweep.
            for _marker_id in _marker_mapping_ids:
                with contextlib.suppress(SQLAlchemyError):
                    async with self.runtime.sessionmaker() as _clear_session:
                        await clear_active_turn(_clear_session, id=_marker_id)
                        await _clear_session.commit()

            # Flush any task-continuation queued for this thread (e.g. by a
            # handoff tool call earlier in THIS turn). The common case is an
            # empty list. This thread's `_processing` slot is already held by
            # `_orchestrate` for the whole turn, so the unguarded entry is the
            # right one here -- `dispatch_continuations_in_thread` takes the
            # guard and is for callers outside a turn.
            await self._dispatch_continuations(
                web_client=web_client,
                tenant_id=tenant_id,
                channel=channel,
                thread_id=thread_id,
                account_id=admission.account_id,
                team_id=team_id,
            )

            # Detached output sweep. `outcome.ma_session_id` is the post-recovery
            # session id, so outputs stranded in a dead session are not
            # recoverable. A detached sweep is not counted by drain_and_close's
            # `_processing` poll, so SIGTERM can kill one mid-flight —
            # post-then-delete makes that self-healing (redelivered on the next
            # turn's sweep, or double-posted inside the already-accepted
            # post-then-delete crash window).
            if not any(isinstance(block, ToolUseBlock) for block in outcome.state.content):
                return
            # Read the chain link synchronously, before spawning, so it cannot be lost.
            previous = self._output_sweeps.get(outcome.ma_session_id)
            task = self._spawn(
                self._sweep_session_outputs(
                    previous,
                    web_client,
                    session_id=outcome.ma_session_id,
                    channel_id=channel,
                    thread_ts=thread_id,
                    team_id=team_id,
                )
            )
            self._output_sweeps[outcome.ma_session_id] = task
            task.add_done_callback(
                functools.partial(self._forget_output_sweep, outcome.ma_session_id)
            )
        finally:
            # Bookkeeping only -- see the comment above the try. Deregister
            # first (idempotent pop), then clear the marker on every mapping
            # row it could be stranded on, each id suppressed independently
            # so one failed clear cannot skip the other row.
            if lifecycle.status_ts is not None:
                self._deregister_cancel(lifecycle.status_ts, team_id=team_id, channel=channel)
            intent_terminal = intent_terminal or lifecycle_holder[0].final_ts is not None
            if intent_terminal and lifecycle_holder[0].status_ts is not None:
                try:
                    async with self.runtime.sessionmaker() as intent_session:
                        await retire_turn_card_intent(
                            intent_session,
                            id=card_intent.id,
                            expected_message_id=lifecycle_holder[0].status_ts,
                        )
                        await intent_session.commit()
                except SQLAlchemyError:
                    log.exception(
                        "slack.turn_card_intent.retire_failed",
                        intent_id=str(card_intent.id),
                    )
            for _marker_id in _marker_mapping_ids:
                with contextlib.suppress(SQLAlchemyError):
                    async with self.runtime.sessionmaker() as _clear_session:
                        await clear_active_turn(_clear_session, id=_marker_id)
                        await _clear_session.commit()

    async def _run_continuation_turn(
        self,
        row: TaskContinuationRow,
        seed_user_message: str,
        *,
        web_client: AsyncWebClient,
        tenant_id: uuid.UUID,
        channel: str,
        thread_id: str,
        team_id: str,
    ) -> None:
        with observe_turn(
            self.runtime.sessionmaker,
            tenant_id=tenant_id,
            platform="slack",
            channel_id=channel,
            thread_id=thread_id,
            origin="handoff",
        ):
            return await self._run_continuation_turn_observed(
                row,
                seed_user_message,
                web_client=web_client,
                tenant_id=tenant_id,
                channel=channel,
                thread_id=thread_id,
                team_id=team_id,
            )

    async def _run_continuation_turn_observed(
        self,
        row: TaskContinuationRow,
        seed_user_message: str,
        *,
        web_client: AsyncWebClient,
        tenant_id: uuid.UUID,
        channel: str,
        thread_id: str,
        team_id: str,
    ) -> None:
        """Run the receiving agent's first turn for one dispatched continuation.

        Same path as an ordinary mention (admit -> bind_session ->
        run_prepared_turn), as the requester who asked for it and seeded with
        their own words. A `task_handoff` row is framed by a one-time
        `HandoffNotice` so the receiving agent's first reply shows it has the
        task; a `private_input_applied` row is the SAME agent resuming with a
        value it just asked for, so it gets no notice -- there is no transfer
        to announce and naming a "previous agent" would invent one.

        The outgoing agent's identity is read off the thread's live session
        row BEFORE `bind_session` runs, since that bind is what supersedes it.
        """
        # Read the predecessor BEFORE bind_session decides the replacement --
        # once it runs, the old row is superseded and this is the only chance
        # to read what it was running.
        async with self.runtime.sessionmaker() as _predecessor_session:
            predecessor = await get_live_thread_session(
                _predecessor_session,
                tenant_id=tenant_id,
                platform="slack",
                thread_id=thread_id,
                account_id=row.requester_account_id,
            )
            from_ma_agent_id = predecessor.ma_agent_id if predecessor is not None else None
            asking_ma_agent_id = await load_asking_agent_id(
                _predecessor_session, row, live_ma_agent_id=from_ma_agent_id
            )
        from_name = (
            predecessor.effective_config.agent_name
            if predecessor is not None and predecessor.effective_config is not None
            else None
        )

        # The bot account the mention names, resolved before the card posts.
        # A continuation can be the first turn this process runs for the
        # workspace, so the gate may not have cached it yet. A failed lookup
        # runs the turn without it: the dispatcher settles a raising
        # continuation as failed and never retries it.
        try:
            account = responder_account(await self._bot_user_id(team_id, web_client))
        except (SlackApiError, aiohttp.ClientError, TimeoutError) as exc:
            log.warning(
                "slack.continuation.bot_user_id_unresolved",
                team_id=team_id,
                thread_id=thread_id,
                exc_info=exc,
            )
            account = None
        # The follow-up runs as the requester, so it carries their role as it
        # stands NOW, read from Slack the way a mention reads it. A failed
        # lookup runs the turn as USER -- never more than the requester holds.
        admin_status = await resolve_admin_status(
            web_client, user_id=row.requester_external_user_id
        )
        role = Role.ADMIN if admin_status is True else Role.USER
        follow_admission = await admit(
            self.runtime.turn_deps,
            tenant_id=tenant_id,
            platform="slack",
            external_user_id=row.requester_external_user_id,
            channel_id=channel,
            thread_id=thread_id,
            role=role,
            platform_role_ids=()
            if role is Role.ADMIN
            else sorted(
                await user_group_ids(
                    self.runtime,
                    web_client,
                    tenant_id=tenant_id,
                    user_id=row.requester_external_user_id,
                )
            ),
            now=datetime.now(UTC),
            # A continuation owed to a private DM conversation is a DM turn:
            # outside every pin, with the DM memory rule.
            is_dm=thread_id.startswith(DM_SCOPE_PREFIX),
        )
        # A wake runs only as the agent it was queued for; a thread rerouted in
        # the meantime refuses it here, before any card, bind or billed turn.
        check_wake_responder(
            reason=row.reason,
            target_ma_agent_id=row.target_ma_agent_id,
            target_name=row.target_name,
            admitted_ma_agent_id=follow_admission.agent.id,
            admitted_name=follow_admission.agent.name,
            asking_ma_agent_id=asking_ma_agent_id,
        )
        follow_deadline = turn_deadline(now=datetime.now(UTC))
        follow_prepared = await bind_session(
            self.runtime.turn_deps,
            follow_admission,
            tenant_id=tenant_id,
            platform="slack",
            external_user_id=row.requester_external_user_id,
            thread_id=thread_id,
            session_account_id=follow_admission.account_id,
            reuse_existing=True,
            deadline=follow_deadline,
        )
        async with self.runtime.sessionmaker() as intent_session:
            card_intent = await create_turn_card_intent(
                intent_session,
                tenant_id=tenant_id,
                platform="slack",
                thread_id=thread_id,
                turn_token=uuid.uuid4(),
                channel_id=channel,
            )
            await intent_session.commit()
        follow_cancel = asyncio.Event()
        follow_identity: AgentIdentity | None = None
        try:
            async with self.runtime.sessionmaker.begin() as identity_session:
                follow_identity = await resolve_agent_identity(
                    identity_session,
                    tenant_id=tenant_id,
                    agent_name=follow_admission.agent.name,
                    is_builtin=is_builtin_agent(
                        name=follow_admission.agent.name,
                        metadata=follow_admission.agent.metadata,
                        default_agent_name=self.runtime.deployment_default.agent_name,
                    ),
                    public_base_url=self.runtime.settings.mcp.app_root_url,
                    enabled=identity_enabled_for(self.runtime.settings, "slack", team_id),
                    background_sessionmaker=self.runtime.sessionmaker,
                    wait_for_face=True,
                )
        except (anthropic.APIError, SQLAlchemyError) as exc:
            log.warning("slack.agent_identity_lookup_failed", error_type=type(exc).__name__)
        follow_lifecycle = SlackTurnLifecycle(
            sessionmaker=self.runtime.sessionmaker,
            alert_webhook_url=self.runtime.settings.ops.alert_webhook_url,
            ask_human=slack_support_enabled(self.runtime.settings.support),
            tenant_id=tenant_id,
            budget_channel_id=follow_admission.budget_channel_id,
            render_tables=self.runtime.settings.table_rendering.get(tenant_id, False) is True,
            client=web_client,
            channel=channel,
            thread_ts=thread_id,
            cancel=follow_cancel,
            author_id=row.requester_external_user_id,
            notify_on_completion=self.runtime.settings.completion_pings.get(tenant_id, False)
            is True,
            agent_name=follow_admission.agent.name,
            model_id=follow_admission.agent.model.id,
            markup=self.runtime.turn_deps.markup,
            register=functools.partial(self._register_cancel, team_id=team_id, channel=channel),
            deregister=functools.partial(self._deregister_cancel, team_id=team_id, channel=channel),
            register_pending=functools.partial(
                self._register_cancel, team_id=team_id, channel=channel
            ),
            deregister_pending=functools.partial(
                self._deregister_cancel, team_id=team_id, channel=channel
            ),
            intent_id=card_intent.id,
            identity=follow_identity,
            ma_agent_id=str(follow_admission.agent.id),
        )
        lifecycle_holder: list[SlackTurnLifecycle] = [follow_lifecycle]
        await follow_lifecycle.post_initial()
        try:
            async with self.runtime.sessionmaker() as intent_session:
                recorded = await record_turn_card_message(
                    intent_session,
                    id=card_intent.id,
                    message_id=follow_lifecycle.status_ts or "",
                )
                await intent_session.commit()
            if not recorded:
                raise RuntimeError("Slack continuation card intent could not record its message ID")
        except BaseException:
            if follow_lifecycle.status_ts is not None:
                self._deregister_cancel(
                    follow_lifecycle.status_ts, team_id=team_id, channel=channel
                )
            raise
        if follow_prepared.mapping_id is not None and follow_lifecycle.status_ts is not None:
            async with self.runtime.sessionmaker() as _at_session:
                await mark_turn_active(
                    _at_session,
                    id=follow_prepared.mapping_id,
                    active_turn_message_id=follow_lifecycle.status_ts,
                    active_turn_channel_id=channel,
                    now=datetime.now(UTC),
                )
                await _at_session.commit()

        handoff_notice = (
            build_handoff_notice(
                from_name=from_name,
                from_ma_agent_id=from_ma_agent_id,
                requested_by=f"<@{row.requester_external_user_id}>",
                requested_work=seed_user_message,
                transfer_kind=follow_prepared.continuity.transfer_kind,
            )
            if row.reason == "task_handoff"
            else None
        )

        async def _follow_up_reseed_user_message() -> str:
            async with self.runtime.sessionmaker() as session:
                recovery_origin = await get_active_origin(
                    session,
                    origin_id=follow_origin.id,
                    tenant_id=tenant_id,
                    account_id=follow_admission.account_id,
                    platform="slack",
                    now=datetime.now(UTC),
                )
            if recovery_origin is None:
                raise UserFacingError(
                    "This turn's setup context expired. Please retry your message."
                )
            return (
                render_turn_origin(
                    recovery_origin,
                    responder_handle=responder_handle(self.runtime.settings),
                    responder_account=account,
                    handoff=handoff_notice,
                    is_channel_admin=is_channel_admin,
                )
                + "\n"
                + seed_user_message
            )

        def _follow_up_recovery_lifecycle(cancel: asyncio.Event) -> TurnLifecycle:
            new_lifecycle = SlackTurnLifecycle(
                sessionmaker=self.runtime.sessionmaker,
                alert_webhook_url=self.runtime.settings.ops.alert_webhook_url,
                ask_human=slack_support_enabled(self.runtime.settings.support),
                tenant_id=tenant_id,
                budget_channel_id=follow_admission.budget_channel_id,
                render_tables=self.runtime.settings.table_rendering.get(tenant_id, False) is True,
                client=web_client,
                channel=channel,
                thread_ts=thread_id,
                cancel=cancel,
                author_id=row.requester_external_user_id,
                notify_on_completion=self.runtime.settings.completion_pings.get(tenant_id, False)
                is True,
                agent_name=follow_admission.agent.name,
                model_id=follow_admission.agent.model.id,
                markup=self.runtime.turn_deps.markup,
                register=functools.partial(self._register_cancel, team_id=team_id, channel=channel),
                deregister=functools.partial(
                    self._deregister_cancel, team_id=team_id, channel=channel
                ),
                register_pending=functools.partial(
                    self._register_cancel, team_id=team_id, channel=channel
                ),
                deregister_pending=functools.partial(
                    self._deregister_cancel, team_id=team_id, channel=channel
                ),
                adopt_status_ts=follow_lifecycle.status_ts,
                header_customized=(
                    follow_lifecycle.header_customized
                    and identity_enabled_for(self.runtime.settings, "slack", team_id)
                ),
                intent_id=card_intent.id,
                identity=follow_identity,
                ma_agent_id=str(follow_admission.agent.id),
            )
            lifecycle_holder[0] = new_lifecycle
            if follow_lifecycle.status_ts is not None:
                self._register_cancel(
                    follow_lifecycle.status_ts,
                    cancel,
                    row.requester_external_user_id,
                    team_id=team_id,
                    channel=channel,
                )
            return new_lifecycle

        is_channel_admin = await holds_current_channel_admin_grant(
            self.runtime.sessionmaker,
            tenant_id=tenant_id,
            account_id=follow_admission.account_id,
            platform="slack",
            parent_channel_id=channel,
            role=role,
        )
        try:
            async with turn_origin(
                self.runtime.sessionmaker,
                tenant_id=tenant_id,
                account_id=follow_admission.account_id,
                platform="slack",
                parent_channel_id=channel,
                thread_id=thread_id,
                responder_ma_agent_id=str(follow_admission.agent.id),
                responder_name=follow_admission.config.agent_name or follow_admission.agent.name,
                configuration_target_ma_agent_id=(
                    follow_admission.config.configuration_target_ma_agent_id
                ),
                configuration_target_name=(follow_admission.config.configuration_target_name),
                role=role,
                is_setup=follow_admission.config.thread_binding_kind == "setup",
            ) as follow_origin:
                await run_prepared_turn(
                    self.runtime.turn_deps,
                    follow_prepared,
                    tenant_id=tenant_id,
                    platform="slack",
                    thread_id=thread_id,
                    external_user_id=row.requester_external_user_id,
                    origin="handoff" if handoff_notice is not None else "chat",
                    user_message=(
                        render_turn_origin(
                            follow_origin,
                            responder_handle=responder_handle(self.runtime.settings),
                            responder_account=account,
                            handoff=handoff_notice,
                            is_channel_admin=is_channel_admin,
                        )
                        + "\n"
                        + seed_user_message
                    ),
                    lifecycle=follow_lifecycle,
                    cancel=follow_cancel,
                    reseed_user_message=_follow_up_reseed_user_message,
                    recovery_lifecycle=_follow_up_recovery_lifecycle,
                    render_interval_s=2.0,
                    deadline=follow_deadline,
                    confirm_write=self._confirmations.hook(
                        web_client,
                        channel=channel,
                        thread_ts=thread_id,
                        identity=follow_identity,
                        record_post=follow_lifecycle.record_post,
                    ),
                )
        finally:
            final_lifecycle = lifecycle_holder[0]
            if final_lifecycle.status_ts is not None:
                self._deregister_cancel(final_lifecycle.status_ts, team_id=team_id, channel=channel)
            if final_lifecycle.final_ts is not None and final_lifecycle.status_ts is not None:
                try:
                    async with self.runtime.sessionmaker() as intent_session:
                        await retire_turn_card_intent(
                            intent_session,
                            id=card_intent.id,
                            expected_message_id=final_lifecycle.status_ts,
                        )
                        await intent_session.commit()
                except SQLAlchemyError:
                    log.exception(
                        "slack.turn_card_intent.retire_failed",
                        intent_id=str(card_intent.id),
                    )
            if follow_prepared.mapping_id is not None:
                with contextlib.suppress(SQLAlchemyError):
                    async with self.runtime.sessionmaker() as _clear_session:
                        await clear_active_turn(_clear_session, id=follow_prepared.mapping_id)
                        await _clear_session.commit()

    async def _dispatch_continuations(
        self,
        *,
        web_client: AsyncWebClient,
        tenant_id: uuid.UUID,
        channel: str,
        thread_id: str,
        account_id: uuid.UUID,
        team_id: str,
    ) -> None:
        """Claim and settle every pending continuation for this thread, guard held.

        Callers must already own this thread's `_processing` slot; the
        turn-completion path does (`_orchestrate` holds it for the whole turn).
        Anything outside a turn goes through `dispatch_continuations_in_thread`,
        which takes the guard first.

        The marker state is read back off the live row rather than passed as a
        constant: `decide_continuation`'s turn-running gate has to reflect the
        row, not the caller's expectation of it (a suppressed clear, or a turn
        that landed on a mapping row the caller never tracked, both leave the
        marker standing).
        """
        async with self.runtime.sessionmaker() as _live_session:
            live_row = await get_live_thread_session(
                _live_session,
                tenant_id=tenant_id,
                platform="slack",
                thread_id=thread_id,
                account_id=account_id,
            )
        await dispatch_pending_continuations(
            self.runtime.sessionmaker,
            self.runtime.anthropic,
            web_client,
            tenant_id=tenant_id,
            channel=channel,
            thread_id=thread_id,
            active_turn=live_row is not None and live_row.active_turn_message_id is not None,
            page_limit=self._history_page_limit(),
            run_follow_up=lambda row, seed: self._run_continuation_turn(
                row,
                seed,
                web_client=web_client,
                tenant_id=tenant_id,
                channel=channel,
                thread_id=thread_id,
                team_id=team_id,
            ),
        )

    async def dispatch_continuations_in_thread(
        self,
        *,
        web_client: AsyncWebClient,
        tenant_id: uuid.UUID,
        channel: str,
        thread_id: str,
        account_id: uuid.UUID,
        team_id: str,
    ) -> None:
        await self._wait_for_orphan_recovery()
        request = dict(
            web_client=web_client,
            tenant_id=tenant_id,
            channel=channel,
            thread_id=thread_id,
            account_id=account_id,
            team_id=team_id,
        )
        thread_key = (team_id, channel, thread_id)
        if not claim_dispatch(
            self._processing,
            thread_key,
            self._deferred_dispatch,
            thread_key,
            request,
        ):
            return
        try:
            await dispatch_and_drain(
                lambda: self._dispatch_continuations(
                    web_client=web_client,
                    tenant_id=tenant_id,
                    channel=channel,
                    thread_id=thread_id,
                    account_id=account_id,
                    team_id=team_id,
                ),
                lambda: self._drain_pending_mentions(
                    channel=channel,
                    web_client=web_client,
                    tenant_id=tenant_id,
                    thread_id=thread_id,
                    team_id=team_id,
                ),
            )
        finally:
            self._release_thread(thread_key)
            await self._notify_undrained_mentions(
                self._pending.pop(thread_key, []),
                channel=channel,
                web_client=web_client,
                thread_id=thread_id,
            )

    def _release_thread(self, thread_key: tuple[str, str, str]) -> None:
        release_thread(
            self._processing,
            thread_key,
            self._deferred_dispatch,
            dispatch_keys=lambda: [thread_key],
            draining=self.draining,
            resume=lambda _key, request: self._spawn(
                self.dispatch_continuations_in_thread(**request)
            ),
        )

    async def drain_and_close(self, client: AsyncBaseSocketModeClient) -> None:
        """Graceful shutdown drain.

        Sets draining=True so new mentions are rejected, waits for acked
        mention handlers and ``_processing`` to drain (or the grace window to
        elapse), then closes the WebSocket client. Stays within the
        deployment's 60s kill timeout.
        """
        self.draining = True
        log.info(
            "slack.draining",
            inflight_threads=len(self._processing),
            inflight_mentions=len(self._mention_tasks) + self._mention_acks_pending,
        )
        deadline = asyncio.get_running_loop().time() + _DRAIN_GRACE_S
        while (
            self._processing or self._mention_tasks or self._mention_acks_pending
        ) and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.5)
        log.info(
            "slack.drain_complete",
            remaining_threads=len(self._processing),
            remaining_mentions=len(self._mention_tasks) + self._mention_acks_pending,
        )
        if self._card_recovery_task is not None and not self._card_recovery_task.done():
            self._card_recovery_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._card_recovery_task
        await client.close()
