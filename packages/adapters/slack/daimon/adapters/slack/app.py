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
from daimon.adapters.slack.billing_panel.actions import handle_billing_command, handle_topup_select
from daimon.adapters.slack.boot_sweep import (
    recover_slack_card_intents,
    retire_orphaned_turns,
    snapshot_slack_card_intents,
)
from daimon.adapters.slack.context import build_context_xml, build_delta_xml
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
from daimon.adapters.slack.errors import generate_request_id, render_error
from daimon.adapters.slack.feedback import (
    evaluate_feedback_text_submission,
    handle_feedback_vote,
    run_feedback_text_submission,
)
from daimon.adapters.slack.gating import is_external_interactive, is_slack_connect_external
from daimon.adapters.slack.help import handle_help_command
from daimon.adapters.slack.interactions import build_retry_handlers, resolve_web_client
from daimon.adapters.slack.lifecycle import SlackTurnLifecycle
from daimon.adapters.slack.memory import handle_memory_command
from daimon.adapters.slack.mrkdwn import escape_mrkdwn
from daimon.adapters.slack.output_delivery import deliver_session_outputs
from daimon.adapters.slack.privacy_panel.actions import (
    handle_privacy_block_action,
    handle_privacy_command,
)
from daimon.adapters.slack.privacy_panel.submit import (
    evaluate_delete_submission,
    run_purge_and_update,
)
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
    resolve_bot_display_name,
    responder_handle,
)
from daimon.adapters.slack.setup_conversations import handle_setup_lifecycle
from daimon.adapters.slack.vision import (
    SlackFile,
    download_as_image_blocks,
    is_vision_image,
)
from daimon.core.continuity.messages import (
    render_current_work_must_finish,
    render_preparation_failed,
    render_replacement_summary,
    render_responder_changed_without_handoff,
    render_unexpected_loss,
)
from daimon.core.credential_requests import SLACK_ACTION_ID as SLACK_CREDENTIAL_ACTION_ID
from daimon.core.defaults.provisioning import teardown_slack_install
from daimon.core.errors import DaimonError
from daimon.core.github_credentials import build_multifernet, decrypt_token
from daimon.core.ma_identity import derive_agent_uuid, derive_tenant_uuid
from daimon.core.ma_resolver import MAResolverMissError
from daimon.core.observability import capture_exception_with_scope
from daimon.core.slack_oauth import build_slack_connect_url
from daimon.core.stores.credential_requests import peek_credential_request
from daimon.core.stores.domain import Role, TaskContinuationRow
from daimon.core.stores.slack_bot_tokens import get_slack_bot_token
from daimon.core.stores.slack_connect_prompts import mark_connect_prompted, was_connect_prompted
from daimon.core.stores.slack_event_dedup import insert_if_new
from daimon.core.stores.slack_turn_contexts import (
    create_slack_turn_context,
    delete_slack_turn_context,
)
from daimon.core.stores.slack_user_tokens import get_slack_user_token
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
from daimon.core.turn.gating import should_admit_turn
from daimon.core.turn.lifecycle import TurnLifecycle
from daimon.core.turn.prepare import ContinuityOutcome, bind_session
from daimon.core.turn.run import run_prepared_turn
from daimon.core.turn.state import ToolUseBlock
from daimon.core.turn_keys import list_mounted_key_names
from daimon.core.turn_origin import HandoffNotice, SessionState, render_turn_origin, turn_origin
from slack_sdk.errors import SlackApiError
from slack_sdk.socket_mode.async_client import AsyncBaseSocketModeClient
from slack_sdk.socket_mode.request import SocketModeRequest
from slack_sdk.socket_mode.response import SocketModeResponse
from slack_sdk.web.async_client import AsyncWebClient
from sqlalchemy.exc import SQLAlchemyError

log = structlog.get_logger()

# Grace window for graceful shutdown drain. Must be strictly less
# than the deployment's 60s kill timeout to leave headroom for client.close()
# and health-server cleanup after the drain completes.
_DRAIN_GRACE_S: float = 50.0

# Recovery gates turn admission, so retry transient failures with a capped
# delay instead of leaving the process permanently unable to turn.
_ORPHAN_RECOVERY_RETRY_DELAY_S: float = 1.0
_ORPHAN_RECOVERY_MAX_RETRY_DELAY_S: float = 30.0

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


def _build_session_state(continuity: ContinuityOutcome) -> SessionState:
    """Turn a bind's `ContinuityOutcome` into the `<turn_controls>` fact block.

    `lost` is derived from `transfer_kind` rather than carried on
    `ContinuityOutcome` directly: a replacement's own transfer degrade ladder
    (full/transcript/history) already says exactly what did not survive, and
    repeating that vocabulary here is what keeps `render_turn_origin`'s
    honesty instruction ("say what is missing before continuing") accurate.
    """
    if continuity.transfer_kind == "transcript":
        lost: tuple[str, ...] = ("working files",)
    elif continuity.transfer_kind == "history":
        lost = ("working files", "earlier conversation")
    else:
        lost = ()
    return SessionState(state=continuity.state, applied=tuple(continuity.applied), lost=lost)


class SlackApp:
    """Socket Mode listener skeleton.

    Owns the ack-first dispatch, pre-turn safety gates, teardown routing,
    and SIGTERM drain.  Turn orchestration is injected via
    ``_orchestrate``.
    """

    def __init__(self, *, runtime: SlackRuntime) -> None:
        self.runtime = runtime
        # Per-thread concurrency state (keys are Slack thread_ts strings).
        self._processing: set[str] = set()
        self._pending: dict[str, list[dict[str, Any]]] = {}
        # Continuation dispatches skipped because the thread was processing,
        # keyed by thread_ts: re-run when the thread is released (see
        # `_release_thread`). Last writer wins; a dispatch reads every pending
        # row for the thread, so one entry is enough.
        self._deferred_dispatch: dict[str, dict[str, Any]] = {}
        # Per-tenant in-flight cap.
        self._inflight: dict[uuid.UUID, int] = {}
        # Background task references (prevent GC before done-callbacks fire).
        self._bg_tasks: set[asyncio.Task[None]] = set()
        # Mention handlers can be acked and running before they acquire a
        # thread's _processing slot. Drain waits for these too, closing that
        # pre-orchestration gap without changing crash recovery semantics.
        self._mention_tasks: set[asyncio.Task[None]] = set()
        self._mention_acks_pending: int = 0
        # Cancel registry: status_ts -> (cancel Event, author_id).
        self._cancel_registry: dict[str, tuple[asyncio.Event, str]] = {}
        # Output-delivery abort-notice dedup, keyed "{team_id}:{error_code}".
        self._delivery_notice_keys: set[str] = set()
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
        is_app_mention = req.type == "events_api" and event_for_ack.get("type") == "app_mention"
        received_before_drain = is_app_mention and not self.draining

        # view_submission acks WITH the computed response_action payload (Pattern 2).
        # evaluate_delete_submission is PURE (no I/O), safe to call before the ack.
        # All other envelope types fall through to the unconditional empty ack below.
        if req.type == "interactive" and payload.get("type") == "view_submission":
            view_vs: dict[str, Any] = payload.get("view") or {}
            cb_id: str = str(view_vs.get("callback_id") or "")
            if cb_id == "privacy_delete":
                decision = evaluate_delete_submission(payload)
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
                        _c: str = _fb_decision.channel_id,
                        _f: str = _fb_decision.feedback_id,
                        _x: str = _fb_decision.text,
                    ) -> None:
                        await run_feedback_text_submission(
                            self.runtime,
                            team_id=_t,
                            user_id=_u,
                            channel_id=_c,
                            feedback_id=_f,
                            text=_x,
                        )

                    self._spawn(_run_feedback_text())
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
            if cmd == "/help":
                self._spawn(handle_help_command(self.runtime, payload))
            elif cmd == "/routines":
                self._spawn(handle_routines_command(self.runtime, payload))
            elif cmd == "/billing":
                self._spawn(handle_billing_command(self.runtime, payload))
            elif cmd == "/privacy":
                self._spawn(handle_privacy_command(self.runtime, payload))
            elif cmd == "/agent-setup":
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
                elif action_id in (
                    "privacy_delete_open",
                    "privacy_export",
                    "privacy_slack_disconnect",
                ):
                    self._spawn(handle_privacy_block_action(self.runtime, payload))
                elif action_id.startswith("agent_setup__"):
                    self._spawn(handle_agent_setup_action(self.runtime, payload))
                elif action_id == SLACK_CREDENTIAL_ACTION_ID:
                    self._spawn(handle_credential_request_click(self.runtime, payload))
                elif action_id.startswith("feedback_vote:"):
                    self._spawn(handle_feedback_vote(self.runtime, payload))
        else:
            # Log unrecognised envelope types so the envelope key can be
            # confirmed or corrected from staging logs (T-82-20).
            log.debug("slack.on_request.unrecognised_envelope_type", req_type=req.type)

    def _register_cancel(self, status_ts: str, cancel: asyncio.Event, author_id: str) -> None:
        """Register a turn's cancel Event in the status_ts-keyed registry."""
        self._cancel_registry[status_ts] = (cancel, author_id)

    def _deregister_cancel(self, status_ts: str) -> None:
        """Remove a turn's cancel registry entry on turn completion."""
        self._cancel_registry.pop(status_ts, None)

    def _release_inflight(self, tenant_id: uuid.UUID) -> None:
        """Release one per-tenant in-flight slot, dropping the key at zero.

        Mirrors Discord's ``_release_inflight`` (bot.py:452-456).
        """
        self._inflight[tenant_id] = self._inflight.get(tenant_id, 1) - 1
        if self._inflight[tenant_id] <= 0:
            self._inflight.pop(tenant_id, None)

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
        entry = self._cancel_registry.get(action_key) or self._cancel_registry.get(status_ts)
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

        Gate order (strict):
        1. Draining check (fast path — no I/O).
        2. DEDUP: insert_if_new before any other work.
        3. TOKEN RESOLVE: get_slack_bot_token; drop on None.
        4. PER-EVENT CLIENT: decrypt + AsyncWebClient(token=...) — never cached.
        5. SLACK CONNECT GATE: ephemeral rejection for external-workspace senders.
        6. TENANT RESOLVE: derive_tenant_uuid.
        7. Handoff to _orchestrate.

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

            # (4) SLACK CONNECT GATE — reject external-workspace senders.
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

            # (5) TENANT RESOLVE.
            tenant_id = derive_tenant_uuid(platform="slack", workspace_id=team_id)

            # (6) Orchestration seam — turn body is delegated here.
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
            # with, so the log line is all that can be done.
            if client is not None:
                with contextlib.suppress(SlackApiError):
                    await client.chat_postMessage(  # pyright: ignore[reportUnknownMemberType]  # slack_sdk **kwargs: Unknown
                        channel=channel,
                        thread_ts=event.get("thread_ts") or event_ts,
                        text=render_error(exc, request_id=request_id),
                    )

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
           and enqueue. No slot consumed.
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
                auth = await web_client.auth_test()  # pyright: ignore[reportUnknownMemberType]  # SDK kwargs
                if event.get("user") == auth.get("user_id"):
                    return

        # (1) Per-thread queue check — before cap so queued mentions don't consume a slot.
        if thread_id in self._processing:
            # Append before awaiting reactions_add so a Slack API error on the
            # reaction call does not drop the enqueued event (WR-05).
            self._pending.setdefault(thread_id, []).append(event)
            with contextlib.suppress(SlackApiError, aiohttp.ClientError, asyncio.TimeoutError):
                await web_client.reactions_add(  # pyright: ignore[reportUnknownMemberType]  # slack_sdk **kwargs: Unknown
                    channel=channel,
                    timestamp=event_ts,
                    name="hourglass_flowing_sand",
                )
            return

        # (2) Per-tenant concurrency cap.
        # Read-check-increment in ONE synchronous span — no await between.
        assert self.runtime.settings.slack is not None, (
            "SlackApp._orchestrate requires slack settings (entrypoint validates at boot)"
        )
        cap = self.runtime.settings.slack.max_concurrent_turns_per_tenant
        count = self._inflight.get(tenant_id, 0)
        if not should_admit_turn(current_in_flight=count, cap=cap):
            # The rejection below is an ephemeral — it appears in no channel
            # history and no API read. This log line is the ONLY server-side
            # trace a shed turn leaves; without it a shed mention is
            # indistinguishable from a dropped event.
            log.info(
                "turn.skipped.concurrency_shed",
                tenant_id=str(tenant_id),
                team_id=team_id,
                channel_id=channel,
                thread_id=thread_id,
                in_flight=count,
                cap=cap,
            )
            await web_client.chat_postEphemeral(  # pyright: ignore[reportUnknownMemberType]
                channel=channel,
                user=str(event.get("user") or ""),
                # Real thread only — a shed root mention has no thread yet.
                thread_ts=event.get("thread_ts"),
                text=(
                    "This workspace has too many chats in flight right now — try again in a moment."
                ),
            )
            return
        self._inflight[tenant_id] = count + 1

        # (3) Run turn + (4) drain loop, (5) finally release.
        self._processing.add(thread_id)
        try:
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
            self._release_thread(thread_id)
            still_pending = self._pending.pop(thread_id, [])
            self._release_inflight(tenant_id)
            await self._notify_undrained_mentions(
                still_pending, channel=channel, web_client=web_client, thread_id=thread_id
            )

    async def _drain_pending_mentions(
        self,
        *,
        channel: str,
        web_client: AsyncWebClient,
        tenant_id: uuid.UUID,
        thread_id: str,
        team_id: str,
    ) -> None:
        """Run the mentions queued (⌛) behind this thread's turn, one turn per author.

        Callers must own the thread's `_processing` slot: a mention turn
        (`_orchestrate`) and an out-of-turn continuation dispatch
        (`dispatch_continuations_in_thread`) both call this before releasing
        it, so a mention queued behind either one gets its own turn.
        """
        # Drain loop: new events may arrive during the drain turn; they land
        # in _pending and are picked up by the next iteration. Each drained
        # turn independently re-enters _run_thread_turn, which admission-gates
        # and bills every turn — new session or reused.
        while queued := self._pending.pop(thread_id, []):
            # Partition by author and run one composite turn per author,
            # in first-seen arrival order. Coalescing distinct authors onto
            # one turn would route B's mention into A's session, under A's
            # vault token and Slack visibility, billed to A, with B's own
            # per-user cap never evaluated. One turn = one caller.
            # Mirrors Discord's _drain_pending_mentions (bot.py:900-914).
            by_user: dict[str, list[dict[str, Any]]] = {}
            for q_event in queued:
                author = _author_id(q_event)
                if not author:
                    # No author to run as. `_run_thread_turn` would resolve a
                    # principal for the empty string and bill a turn to a
                    # phantom account. Discord cannot hit this — a Message
                    # always has an author.
                    log.warning(
                        "slack.drain.skipped_authorless_event",
                        thread_id=thread_id,
                        team_id=team_id,
                    )
                    continue
                by_user.setdefault(author, []).append(q_event)
            for user_events in by_user.values():
                # One author's failure must not consume the others'. Their
                # events are already popped from _pending, so the owner's
                # `_notify_undrained_mentions` cannot reach them — without this they would
                # vanish with no turn and no message. Discord gets the same
                # property for free because `_handle_mention` renders turn
                # errors internally and never raises; `_run_thread_turn`
                # documents the opposite ("errors propagate to the listener
                # boundary"), so Slack has to isolate here.
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
        """Turn body: admission → card+Cancel → bind_session → marker → context → turn → watermark.

        Immediately after admission, the lifecycle is constructed and its
        status card + Cancel registration are posted (``post_initial()``)
        -- before ``bind_session``, since MA ``sessions.create`` plus the
        interstitial history replay and image download below can hold for
        minutes and the user must see something first. Once ``bind_session``
        returns, the turn marker (message ts, channel, start time) is written
        against the mapping row.

        On first mention for a thread: creates a new MA session + ``thread_sessions``
        row, replays thread history via ``build_context_xml`` (one Slack page).
        On follow-up mentions: reuses the existing MA session, replays only the
        delta since the watermark via ``build_delta_xml``.

        One ~45-minute ceiling deadline is computed after admission and shared
        by both the ``bind_session`` and ``run_prepared_turn`` calls below.

        Mirrors Discord ``_orchestrate`` (bot.py:831-1098).
        No try/except — errors propagate to the listener boundary in
        ``_handle_app_mention``.
        """
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
                now=datetime.now(UTC),
            )
        except MissingTurnConfigError as err:
            log.info(
                "slack.missing_config",
                team_id=team_id,
                channel_id=channel,
                missing=list(err.missing),
            )
            hints: list[str] = []
            if "agent" in err.missing:
                hints.append(
                    "Ask a workspace admin to tell Daimon which agent should answer here "
                    "(/agent-setup shows who answers where)."
                )
            if "environment" in err.missing:
                hints.append("Ask the operator to configure an environment for this channel.")
            await web_client.chat_postMessage(  # pyright: ignore[reportUnknownMemberType]
                channel=channel,
                thread_ts=thread_id,
                text=(
                    f"No {' or '.join(err.missing)} configured for this channel. " + " ".join(hints)
                ),
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
                text=(
                    "The configured agent or environment no longer exists. "
                    "Ask a workspace admin to ask Daimon for an existing agent, "
                    "or ask the operator to restore the environment."
                ),
            )
            return
        except AdmissionDenied as err:
            if err.reason == "balance_depleted":
                log.info(
                    "turn.skipped.over_balance",
                    tenant_id=str(tenant_id),
                    team_id=team_id,
                    channel_id=channel,
                    thread_id=thread_id,
                )
                await web_client.chat_postMessage(  # pyright: ignore[reportUnknownMemberType]
                    channel=channel,
                    thread_ts=thread_id,
                    text=(
                        f"This workspace's "
                        f"{escape_mrkdwn(resolve_bot_display_name(self.runtime.settings))} "
                        "credit is depleted. "
                        "An admin can top up with `/billing`."
                    ),
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
                await web_client.chat_postMessage(  # pyright: ignore[reportUnknownMemberType]
                    channel=channel,
                    thread_ts=thread_id,
                    text=(
                        "Monthly usage cap reached for this workspace. "
                        "An admin can adjust the cap with `/billing` (when available)."
                    ),
                )
            return

        agent = admission.agent
        _lc_agent_name: str = agent.name
        _lc_model_id: str = agent.model.id

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
            client=web_client,
            channel=channel,
            thread_ts=thread_id,
            cancel=cancel_event,
            author_id=str(event.get("user") or ""),
            agent_name=_lc_agent_name,
            model_id=_lc_model_id,
            register=self._register_cancel,
            deregister=self._deregister_cancel,
            register_pending=self._register_cancel,
            deregister_pending=self._deregister_cancel,
            intent_id=card_intent.id,
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
                self._deregister_cancel(lifecycle.status_ts)
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
            # recorder binding) -- D-01 bind_session(). Slack has no
            # per_caller_thread_sessions equivalent: session_account_id is always
            # the admitted caller's account, and threads always pre-exist. ---
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
                    await web_client.chat_postMessage(  # pyright: ignore[reportUnknownMemberType]  # SDK kwargs
                        channel=channel,
                        thread_ts=thread_id,
                        text=explanation,
                    )
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
                    await web_client.chat_postMessage(  # pyright: ignore[reportUnknownMemberType]  # SDK kwargs
                        channel=channel,
                        thread_ts=thread_id,
                        text=busy_text,
                    )
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
                )
                if lifecycle.status_ts is not None:
                    await web_client.chat_update(  # pyright: ignore[reportUnknownMemberType]  # SDK kwargs
                        channel=channel,
                        ts=lifecycle.status_ts,
                        text=explanation,
                        blocks=[],
                    )
                    intent_terminal = True
                else:
                    await web_client.chat_postMessage(  # pyright: ignore[reportUnknownMemberType]  # SDK kwargs
                        channel=channel,
                        thread_ts=thread_id,
                        text=explanation,
                    )
                return
            ma_session_id = prepared.ma_session_id
            watermark = prepared.watermark
            reused = prepared.reused

            # Continuity facts about this bind, for `<turn_controls>` and the
            # pre-answer notices below. None on the ordinary "nothing changed"
            # path so a plain turn's controls are byte-identical to before
            # this existed.
            session_state = (
                _build_session_state(prepared.continuity)
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
                prepared.continuity.state == "replaced"
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
            if not reused:
                # First turn: replay thread history (one Slack page from the root).
                user_message = await build_context_xml(
                    web_client,
                    channel=channel,
                    thread_ts=thread_id,
                    user_query=user_text,
                    author_id=author_id,
                    is_admin=is_admin,
                    proxy=proxy_ctx,
                    key_names=key_names,
                )
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
                    await web_client.chat_postMessage(  # pyright: ignore[reportUnknownMemberType]
                        channel=channel,
                        thread_ts=thread_id,
                        text=(
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
                full_message = await build_context_xml(
                    web_client,
                    channel=channel,
                    thread_ts=thread_id,
                    user_query=user_text,
                    author_id=author_id,
                    is_admin=is_admin,
                    proxy=proxy_ctx,
                    key_names=key_names,
                )
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
                    raise DaimonError(
                        "This turn's setup context expired. Please retry your message."
                    )
                return (
                    render_turn_origin(
                        recovery_origin,
                        responder_handle=responder_handle(self.runtime.settings),
                        session_state=session_state,
                    )
                    + "\n"
                    + full_message
                )

            def _recovery_lifecycle(cancel: asyncio.Event) -> TurnLifecycle:
                new_lifecycle = SlackTurnLifecycle(
                    client=web_client,
                    channel=channel,
                    thread_ts=thread_id,
                    cancel=cancel,
                    author_id=str(event.get("user") or ""),
                    agent_name=_lc_agent_name,
                    model_id=_lc_model_id,
                    register=self._register_cancel,
                    deregister=self._deregister_cancel,
                    register_pending=self._register_cancel,
                    deregister_pending=self._deregister_cancel,
                    # Take over the failed attempt's card so it is edited into
                    # this turn's answer rather than left standing beside a
                    # second, successful card.
                    adopt_status_ts=lifecycle.status_ts,
                    intent_id=card_intent.id,
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
                    self._register_cancel(lifecycle.status_ts, cancel, str(event.get("user") or ""))
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
                                session_state=session_state,
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
                    await web_client.chat_postMessage(  # pyright: ignore[reportUnknownMemberType]
                        channel=channel, thread_ts=thread_id, text=loss_notice
                    )
            if replacement_summary is not None and not final_lifecycle.answer_prefix_applied:
                # The turn produced no answer to carry the summary (tool-only,
                # cancelled, or failed). The person still has to be told what
                # the replacement carried across, so it goes out on its own.
                await web_client.chat_postMessage(  # pyright: ignore[reportUnknownMemberType]
                    channel=channel, thread_ts=thread_id, text=replacement_summary
                )

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
                await web_client.chat_postMessage(  # pyright: ignore[reportUnknownMemberType]
                    channel=channel,
                    thread_ts=thread_id,
                    text=render_current_work_must_finish(admission.agent.name, handoff=False),
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
                self._deregister_cancel(lifecycle.status_ts)
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
        from_name = (
            predecessor.effective_config.agent_name
            if predecessor is not None and predecessor.effective_config is not None
            else None
        )

        follow_admission = await admit(
            self.runtime.turn_deps,
            tenant_id=tenant_id,
            platform="slack",
            external_user_id=row.requester_external_user_id,
            channel_id=channel,
            thread_id=thread_id,
            role=Role.USER,
            now=datetime.now(UTC),
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
        follow_lifecycle = SlackTurnLifecycle(
            client=web_client,
            channel=channel,
            thread_ts=thread_id,
            cancel=follow_cancel,
            author_id=row.requester_external_user_id,
            agent_name=follow_admission.agent.name,
            model_id=follow_admission.agent.model.id,
            register=self._register_cancel,
            deregister=self._deregister_cancel,
            register_pending=self._register_cancel,
            deregister_pending=self._deregister_cancel,
            intent_id=card_intent.id,
        )
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
                self._deregister_cancel(follow_lifecycle.status_ts)
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

        transfer_kind = follow_prepared.continuity.transfer_kind
        workspace: Literal["transferred", "transcript_only", "history_only"]
        not_carried: tuple[str, ...]
        if transfer_kind == "full":
            workspace, not_carried = "transferred", ()
        elif transfer_kind == "transcript":
            workspace, not_carried = "transcript_only", ("working files",)
        else:
            workspace, not_carried = (
                "history_only",
                ("working files", "earlier conversation"),
            )
        handoff_notice = (
            HandoffNotice(
                from_name=from_name or "the previous agent",
                from_ma_agent_id=from_ma_agent_id or "",
                requested_by=f"<@{row.requester_external_user_id}>",
                requested_work=seed_user_message,
                workspace=workspace,
                not_carried=not_carried,
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
                raise DaimonError("This turn's setup context expired. Please retry your message.")
            return (
                render_turn_origin(
                    recovery_origin,
                    responder_handle=responder_handle(self.runtime.settings),
                    handoff=handoff_notice,
                )
                + "\n"
                + seed_user_message
            )

        def _follow_up_recovery_lifecycle(cancel: asyncio.Event) -> TurnLifecycle:
            new_lifecycle = SlackTurnLifecycle(
                client=web_client,
                channel=channel,
                thread_ts=thread_id,
                cancel=cancel,
                author_id=row.requester_external_user_id,
                agent_name=follow_admission.agent.name,
                model_id=follow_admission.agent.model.id,
                register=self._register_cancel,
                deregister=self._deregister_cancel,
                register_pending=self._register_cancel,
                deregister_pending=self._deregister_cancel,
                adopt_status_ts=follow_lifecycle.status_ts,
            )
            if follow_lifecycle.status_ts is not None:
                self._register_cancel(
                    follow_lifecycle.status_ts, cancel, row.requester_external_user_id
                )
            return new_lifecycle

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
                role=Role.USER,
                is_setup=follow_admission.config.thread_binding_kind == "setup",
            ) as follow_origin:
                await run_prepared_turn(
                    self.runtime.turn_deps,
                    follow_prepared,
                    tenant_id=tenant_id,
                    platform="slack",
                    thread_id=thread_id,
                    external_user_id=row.requester_external_user_id,
                    user_message=(
                        render_turn_origin(
                            follow_origin,
                            responder_handle=responder_handle(self.runtime.settings),
                            handoff=handoff_notice,
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
                )
        finally:
            if follow_lifecycle.status_ts is not None:
                self._deregister_cancel(follow_lifecycle.status_ts)
            if follow_lifecycle.final_ts is not None and follow_lifecycle.status_ts is not None:
                try:
                    async with self.runtime.sessionmaker() as intent_session:
                        await retire_turn_card_intent(
                            intent_session,
                            id=card_intent.id,
                            expected_message_id=follow_lifecycle.status_ts,
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
            run_follow_up=lambda row, seed: self._run_continuation_turn(
                row,
                seed,
                web_client=web_client,
                tenant_id=tenant_id,
                channel=channel,
                thread_id=thread_id,
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
        """Claim and run any pending continuations for this thread, from outside a turn.

        Takes the same per-thread guard a mention takes, so a form submission
        and a mention can never dispatch the same thread at once. A thread
        already processing is skipped outright rather than queued: the turn
        running there reaches `_dispatch_continuations` at its own tail anyway,
        and will pick up whatever this call would have.
        """
        await self._wait_for_orphan_recovery()
        if thread_id in self._processing:
            # The turn running here may already be past its own tail
            # dispatch, so remember the call and re-run it on release.
            self._deferred_dispatch[thread_id] = {
                "web_client": web_client,
                "tenant_id": tenant_id,
                "channel": channel,
                "thread_id": thread_id,
                "account_id": account_id,
                "team_id": team_id,
            }
            return
        self._processing.add(thread_id)
        try:
            await self._dispatch_continuations(
                web_client=web_client,
                tenant_id=tenant_id,
                channel=channel,
                thread_id=thread_id,
                account_id=account_id,
            )
            # A mention that arrived during the dispatch queued behind it (⌛);
            # it gets its own turn here, as it would behind a mention turn.
            await self._drain_pending_mentions(
                channel=channel,
                web_client=web_client,
                tenant_id=tenant_id,
                thread_id=thread_id,
                team_id=team_id,
            )
        finally:
            self._release_thread(thread_id)
            await self._notify_undrained_mentions(
                self._pending.pop(thread_id, []),
                channel=channel,
                web_client=web_client,
                thread_id=thread_id,
            )

    def _release_thread(self, thread_id: str) -> None:
        """Free the thread's `_processing` slot; re-run a dispatch it skipped.

        `dispatch_continuations_in_thread` skips a processing thread, trusting
        the running turn's tail dispatch. A continuation recorded after that
        tail already ran (a form submitted as the turn was finishing) would
        otherwise wait for the next completed turn in the thread. Spawned, so
        the caller's `finally` never blocks; not while draining, when no new
        turn may start (the row stays pending for the next turn).
        """
        self._processing.discard(thread_id)
        deferred = self._deferred_dispatch.pop(thread_id, None)
        if deferred is not None and not self.draining:
            self._spawn(self.dispatch_continuations_in_thread(**deferred))

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
