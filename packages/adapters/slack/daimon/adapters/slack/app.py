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
import time
import uuid
from collections.abc import Coroutine
from datetime import UTC, datetime
from typing import Any, cast

import aiohttp
import anthropic
import structlog
from cryptography.fernet import InvalidToken
from daimon.adapters.slack.agent_setup.actions import (
    handle_agent_setup_action,
    handle_agent_setup_command,
)
from daimon.adapters.slack.agent_setup.state import decode_private_metadata
from daimon.adapters.slack.agent_setup.submit import (
    evaluate_add_mcp_submission,
    evaluate_add_skill_submission,
    evaluate_edit_agent_submission,
    evaluate_edit_repo_submission,
    evaluate_fork_agent_submission,
    evaluate_new_agent_submission,
    evaluate_paste_secrets_submission,
    run_add_mcp_submission,
    run_add_skill_submission,
    run_edit_agent_submission,
    run_edit_repo_submission,
    run_fork_agent_submission,
    run_new_agent_submission,
    run_paste_secrets_submission,
)
from daimon.adapters.slack.attachments import (
    ProxyUrlContext,
    build_attachment_url_prefix,
    build_image_url_prefix,
    build_skipped_image_prefix,
)
from daimon.adapters.slack.billing_panel.actions import handle_billing_command, handle_topup_select
from daimon.adapters.slack.context import build_context_xml, build_delta_xml
from daimon.adapters.slack.gating import is_external_interactive, is_slack_connect_external
from daimon.adapters.slack.help import handle_help_command
from daimon.adapters.slack.interactions import resolve_web_client
from daimon.adapters.slack.lifecycle import SlackTurnLifecycle
from daimon.adapters.slack.memory import handle_memory_command
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
from daimon.adapters.slack.runtime import SlackRuntime
from daimon.adapters.slack.vision import (
    SlackFile,
    download_as_image_blocks,
    is_vision_image,
)
from daimon.core.defaults.provisioning import reconcile_tenant_defaults, teardown_slack_install
from daimon.core.errors import DaimonError
from daimon.core.github_credentials import build_multifernet, decrypt_token
from daimon.core.ma_identity import derive_agent_uuid, derive_tenant_uuid
from daimon.core.ma_resolver import (
    MAResolverMissError,
    new_resolver_cache,
    resolve_agent,
    resolve_environment,
)
from daimon.core.observability import capture_exception_with_scope
from daimon.core.scope import ScopeContext
from daimon.core.sessions import create_session
from daimon.core.slack_oauth import build_slack_connect_url
from daimon.core.stores.identity import get_or_create_platform_principal
from daimon.core.stores.scoped_config_read import resolve as resolve_config
from daimon.core.stores.slack_bot_tokens import get_slack_bot_token
from daimon.core.stores.slack_connect_prompts import mark_connect_prompted, was_connect_prompted
from daimon.core.stores.slack_event_dedup import insert_if_new
from daimon.core.stores.slack_turn_contexts import (
    create_slack_turn_context,
    delete_slack_turn_context,
)
from daimon.core.stores.slack_user_tokens import get_slack_user_token
from daimon.core.stores.thread_sessions import (
    create_thread_session,
    get_live_thread_session,
    update_watermark,
)
from daimon.core.turn.driver import run_turn
from daimon.core.turn.gating import should_admit_turn
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


def _log_bg_task_exception(task: asyncio.Task[None]) -> None:
    """Done-callback: surface escaped background-task exceptions immediately
    instead of asyncio's GC-time 'Task exception was never retrieved'."""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        log.error("bg_task_failed", task_name=task.get_name(), exc_info=exc)


def _compose_queued_content(events: list[dict[str, Any]]) -> str:
    """Compose pending mention texts into a single composite user message.

    Single-author: texts joined by blank lines so the model sees them as
    one continuing thought from the same speaker. Multi-author: each prefixed
    with ``[user_id]: `` so the agent can attribute who said what.
    Mirrors Discord's ``_compose_queued_content`` (bot.py:102-114).
    """
    if not events:
        return ""
    user_ids = {str(e.get("user", "")) for e in events}
    if len(user_ids) == 1:
        return "\n\n".join(str(e.get("text", "")) for e in events)
    return "\n\n".join(f"[{e.get('user', '')}]: {e.get('text', '')}" for e in events)


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
        self.runtime = runtime
        # Per-thread concurrency state (keys are Slack thread_ts strings).
        self._processing: set[str] = set()
        self._pending: dict[str, list[dict[str, Any]]] = {}
        # Per-tenant in-flight cap.
        self._inflight: dict[uuid.UUID, int] = {}
        # Background task references (prevent GC before done-callbacks fire).
        self._bg_tasks: set[asyncio.Task[None]] = set()
        # Cancel registry: status_ts -> (cancel Event, author_id).
        self._cancel_registry: dict[str, tuple[asyncio.Event, str]] = {}
        # Drain flag — set on SIGTERM; blocks new mention handling.
        self.draining: bool = False

    def _spawn(self, coro: Coroutine[Any, Any, None]) -> asyncio.Task[None]:
        """Fire-and-forget a background task, tracked so it isn't GC'd."""
        task = asyncio.create_task(coro)
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)
        task.add_done_callback(_log_bg_task_exception)
        return task

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
            elif cb_id in {
                "agent_setup__new_agent",
                "agent_setup__fork_agent",
                "agent_setup__edit_agent",
                "agent_setup__edit_repo",
                "agent_setup__add_skill",
                "agent_setup__add_mcp",
                "agent_setup__paste_secrets",
            }:
                # Pure evaluate (no I/O) — must run before the single ack.
                if cb_id == "agent_setup__new_agent":
                    _as_decision = evaluate_new_agent_submission(payload)
                elif cb_id == "agent_setup__fork_agent":
                    _as_decision = evaluate_fork_agent_submission(payload)
                elif cb_id == "agent_setup__edit_agent":
                    _as_decision = evaluate_edit_agent_submission(payload)
                elif cb_id == "agent_setup__edit_repo":
                    _as_decision = evaluate_edit_repo_submission(payload)
                elif cb_id == "agent_setup__add_skill":
                    _as_decision = evaluate_add_skill_submission(payload)
                elif cb_id == "agent_setup__add_mcp":
                    _as_decision = evaluate_add_mcp_submission(payload)
                else:  # agent_setup__paste_secrets
                    _as_decision = evaluate_paste_secrets_submission(payload)
                await (
                    client.send_socket_mode_response(  # ACK WITH PAYLOAD — pure call above, no I/O
                        SocketModeResponse(
                            envelope_id=req.envelope_id,
                            payload=_as_decision.response_payload,
                        )
                    )
                )
                if _as_decision.proceed:
                    _as_team_info: dict[str, Any] = payload.get("team") or {}
                    _as_team_id: str = str(_as_team_info.get("id") or "")
                    _as_user_info: dict[str, Any] = payload.get("user") or {}
                    _as_user_id: str = str(_as_user_info.get("id") or "")
                    _as_view_info: dict[str, Any] = payload.get("view") or {}
                    _as_view_id: str = str(_as_view_info.get("id") or "")
                    # view_submission payloads carry no top-level "channel" — the
                    # invoking channel lives in the view's private_metadata (encoded
                    # by the L1/L3 builders). Source it from there so success/refuse
                    # ephemerals reach a real channel instead of "" (channel_not_found).
                    _as_meta = decode_private_metadata(
                        str(_as_view_info.get("private_metadata") or "")
                    )
                    _as_channel_id: str = str(_as_meta.get("channel_id") or "")
                    _as_agent_name: str = _as_decision.agent_name or ""
                    _as_parent_section: str = _as_decision.parent_section or ""
                    _as_extra: dict[str, Any] = _as_decision.extra
                    _as_cb_id: str = cb_id

                    async def _run_agent_setup_submission(
                        *,
                        _t: str = _as_team_id,
                        _u: str = _as_user_id,
                        _c: str = _as_channel_id,
                        _v: str = _as_view_id,
                        _a: str = _as_agent_name,
                        _s: str = _as_parent_section,
                        _e: dict[str, Any] = _as_extra,
                        _cb: str = _as_cb_id,
                    ) -> None:
                        wc = await resolve_web_client(self.runtime, team_id=_t)
                        if wc is None:
                            return
                        if _cb == "agent_setup__new_agent":
                            await run_new_agent_submission(
                                self.runtime,
                                wc,
                                team_id=_t,
                                user_id=_u,
                                channel_id=_c,
                                view_id=_v,
                                extra=_e,
                            )
                        elif _cb == "agent_setup__fork_agent":
                            await run_fork_agent_submission(
                                self.runtime,
                                wc,
                                team_id=_t,
                                user_id=_u,
                                channel_id=_c,
                                view_id=_v,
                                extra=_e,
                            )
                        elif _cb == "agent_setup__edit_agent":
                            await run_edit_agent_submission(
                                self.runtime,
                                wc,
                                team_id=_t,
                                user_id=_u,
                                channel_id=_c,
                                view_id=_v,
                                agent_name=_a,
                                parent_section=_s,
                                extra=_e,
                            )
                        elif _cb == "agent_setup__edit_repo":
                            await run_edit_repo_submission(
                                self.runtime,
                                wc,
                                team_id=_t,
                                user_id=_u,
                                channel_id=_c,
                                view_id=_v,
                                agent_name=_a,
                                parent_section=_s,
                                extra=_e,
                            )
                        elif _cb == "agent_setup__add_skill":
                            await run_add_skill_submission(
                                self.runtime,
                                wc,
                                team_id=_t,
                                user_id=_u,
                                channel_id=_c,
                                view_id=_v,
                                agent_name=_a,
                                parent_section=_s,
                                extra=_e,
                            )
                        elif _cb == "agent_setup__add_mcp":
                            await run_add_mcp_submission(
                                self.runtime,
                                wc,
                                team_id=_t,
                                user_id=_u,
                                channel_id=_c,
                                view_id=_v,
                                agent_name=_a,
                                parent_section=_s,
                                extra=_e,
                            )
                        else:  # agent_setup__paste_secrets
                            await run_paste_secrets_submission(
                                self.runtime,
                                wc,
                                team_id=_t,
                                user_id=_u,
                                channel_id=_c,
                                view_id=_v,
                                agent_name=_a,
                                parent_section=_s,
                                extra=_e,
                            )

                    self._spawn(_run_agent_setup_submission())
            else:
                # Unknown view_submission callback_id — log and ack empty (T-82-20).
                log.info("slack.on_request.unknown_view_submission_callback", callback_id=cb_id)
                await client.send_socket_mode_response(
                    SocketModeResponse(envelope_id=req.envelope_id)
                )
            return  # view_submission fully handled

        # ACK FIRST for all non-view_submission envelope types.
        await client.send_socket_mode_response(  # ACK FIRST — no I/O before this line
            SocketModeResponse(envelope_id=req.envelope_id)
        )

        if req.type == "events_api":
            event: dict[str, Any] = payload.get("event") or {}
            team_id_raw = payload.get("team_id") or event.get("team")
            team_id: str = str(team_id_raw) if team_id_raw is not None else ""
            etype: str | None = event.get("type")
            if etype == "app_mention":
                self._spawn(self._handle_app_mention(event, team_id=team_id))
            elif etype in ("app_uninstalled", "tokens_revoked"):
                self._spawn(self._handle_teardown(team_id=team_id))
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

    async def _handle_teardown(self, *, team_id: str) -> None:
        """Route app_uninstalled / tokens_revoked to teardown_slack_install.

        Soft-archives the tenant and deletes the bot-token row so subsequent
        events see no token and are dropped cleanly.
        """
        await teardown_slack_install(
            self.runtime.sessionmaker,
            team_id=team_id,
            now=datetime.now(UTC),
        )

    async def _handle_block_action(self, payload: dict[str, Any]) -> None:
        """Author-gated cancel handler for block_actions interactive payloads.

        Looks up the action's status_ts in _cancel_registry; if the clicker is
        the turn's original author, sets the cancel Event so the driver
        cancel-race loop picks it up.  Non-author clicks and missing registry
        entries are silent no-ops.
        """
        actions: list[dict[str, Any]] = payload.get("actions") or []
        if not actions or actions[0].get("action_id") != "cancel_turn":
            return
        container: dict[str, Any] | None = payload.get("container")
        status_ts: str = (container.get("message_ts") if container is not None else "") or ""
        user_info: dict[str, Any] | None = payload.get("user")
        clicker: str = (user_info.get("id") if user_info is not None else "") or ""
        entry = self._cancel_registry.get(status_ts)
        if entry is None:
            return  # turn already ended / deregistered (Pitfall 6/7)
        cancel, author_id = entry
        if clicker != author_id:
            return  # author gate — silent no-op
        cancel.set()

    async def _handle_app_mention(
        self,
        event: dict[str, Any],
        *,
        team_id: str,
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
        if self.draining:
            # Mentions that arrive during the drain window are acked by on_request
            # (ack-first is unconditional) but dropped here before the dedup insert.
            # Slack considers them delivered; they will not be redelivered to the
            # replacement instance — this is inherent to ack-first + drain (IN-02).
            return

        channel: str = event.get("channel") or ""
        event_ts: str = event.get("event_ts") or event.get("ts") or ""

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
                log.warning("slack.event_dropped.no_token", team_id=team_id)
                return

            # (3) PER-EVENT CLIENT — decrypt and construct; NEVER cache on self/runtime.
            fernet = build_multifernet(
                tuple(k.get_secret_value() for k in self.runtime.settings.crypto.keys)
            )
            token = decrypt_token(fernet, row.encrypted_token)
            client = AsyncWebClient(token=token)  # per-event only

            # (4) SLACK CONNECT GATE — reject external-workspace senders.
            if is_slack_connect_external(event, team_id=team_id):
                await client.chat_postEphemeral(  # pyright: ignore[reportUnknownMemberType]  # slack_sdk **kwargs: Unknown
                    channel=channel,
                    user=event.get("user") or "",
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
            log.error(
                "slack.handle_app_mention_failed",
                team_id=team_id,
                channel=channel,
                event_ts=event_ts,
                exc_info=exc,
            )
            capture_exception_with_scope(exc)

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
                "👋 Tip: connect your Slack account and daimon can read any channel "
                "or DM *you* can see — no invites needed — plus search your messages "
                f"(in a DM with daimon).\nConnect: {connect_url}\n"
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
            await web_client.chat_postEphemeral(  # pyright: ignore[reportUnknownMemberType]
                channel=channel,
                user=str(event.get("user") or ""),
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
            # Drain loop: new events may arrive during the drain turn; they land
            # in _pending and are picked up by the next iteration.
            while queued := self._pending.pop(thread_id, []):
                composite = _compose_queued_content(queued)
                representative = queued[0]
                await self._run_thread_turn(
                    representative,
                    channel=channel,
                    web_client=web_client,
                    tenant_id=tenant_id,
                    thread_id=thread_id,
                    content_override=composite,
                    team_id=team_id,
                    files=_collect_files(queued),
                )
        finally:
            self._processing.discard(thread_id)
            still_pending = self._pending.pop(thread_id, [])
            self._release_inflight(tenant_id)
            # Notify any mentions that were queued (⌛) but never drained because
            # the turn or drain loop raised.  Best-effort: ignore posting failures.
            for queued_event in still_pending:
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
        """Turn body: principal → MA session find-or-create → context build → turn → watermark.

        On first mention for a thread: creates a new MA session + ``thread_sessions``
        row, replays full thread history via ``build_context_xml`` (limit 100).
        On follow-up mentions: reuses the existing MA session, replays only the
        delta since the watermark via ``build_delta_xml``.

        Mirrors Discord ``_orchestrate`` (bot.py:831-1098).
        No try/except — errors propagate to the listener boundary in
        ``_handle_app_mention``.
        """
        # --- Identity ---
        async with self.runtime.sessionmaker() as s:
            principal = await get_or_create_platform_principal(
                s,
                tenant_id=tenant_id,
                platform="slack",
                external_id=str(event.get("user") or ""),
            )
            await s.commit()

        # --- Session find-or-create ---
        async with self.runtime.sessionmaker() as s:
            existing = await get_live_thread_session(
                s,
                tenant_id=tenant_id,
                platform="slack",
                thread_id=thread_id,
                account_id=principal.account_id,
            )

        ma_session_id: str
        mapping_id: uuid.UUID
        watermark: str | None
        reused: bool
        # Populated in the new-session path; placeholder strings on session reuse until
        # the full app wiring (registry, model info for reused sessions) lands.
        _lc_agent_name: str = ""
        _lc_model_id: str = ""

        if existing is not None:
            ma_session_id = existing.ma_session_id
            mapping_id = existing.id
            watermark = existing.watermark_message_id
            reused = True
            log.info(
                "slack.session.reused",
                session_id=ma_session_id,
                thread_id=thread_id,
                watermark=watermark,
            )
        else:
            # Create a fresh MA session (mirror bot.py:852-871).
            # --- Config resolution (mirror bot.py:776-815): channel → tenant →
            # deployment cascade, so /agent-setup propagations take effect here.
            async with self.runtime.sessionmaker() as s:
                config = await resolve_config(
                    s,
                    context=ScopeContext(
                        tenant_id=tenant_id,
                        channel_id=channel,
                        account_id=principal.account_id,
                    ),
                    default=self.runtime.deployment_default,
                )
            agent_tag = config.agent_name
            environment_tag = config.environment_name
            if agent_tag is None or environment_tag is None:
                missing = [
                    name
                    for name, value in (("agent", agent_tag), ("environment", environment_tag))
                    if value is None
                ]
                log.info(
                    "slack.missing_config",
                    team_id=team_id,
                    channel_id=channel,
                    missing=missing,
                )
                hints: list[str] = []
                if agent_tag is None:
                    hints.append(
                        "An admin can set the default agent in `/agent-setup` → "
                        "*Set as default…* → [This channel] or [Whole workspace]."
                    )
                if environment_tag is None:
                    hints.append(
                        "Environment is operator-only — an operator can set it via the CLI "
                        "(`daimon config set environment_name=...`)."
                    )
                await web_client.chat_postMessage(  # pyright: ignore[reportUnknownMemberType]
                    channel=channel,
                    thread_ts=thread_id,
                    text=(
                        f"No {' or '.join(missing)} configured for this channel. " + " ".join(hints)
                    ),
                )
                return

            resolver_cache = new_resolver_cache()
            public_url = (
                str(self.runtime.settings.mcp.public_url)
                if self.runtime.settings.mcp.public_url is not None
                else None
            )

            async def _apply() -> object:
                return await reconcile_tenant_defaults(
                    self.runtime.anthropic,
                    self.runtime.settings.defaults_root,
                    tenant_id=tenant_id,
                    public_url=public_url,
                )

            try:
                agent_id = await resolve_agent(
                    self.runtime.anthropic,
                    tenant_id=tenant_id,
                    daimon_tag=agent_tag,
                    apply_callable=_apply,
                    cache=resolver_cache,
                    cached_id=None,
                )
                env_id = await resolve_environment(
                    self.runtime.anthropic,
                    tenant_id=tenant_id,
                    daimon_tag=environment_tag,
                    apply_callable=_apply,
                    cache=resolver_cache,
                    cached_id=None,
                )
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
                        "An admin can re-set the agent in `/agent-setup` → "
                        "*Set as default…*; the environment is operator-only via the CLI "
                        "(`daimon config set environment_name=...`)."
                    ),
                )
                return
            agent = await self.runtime.anthropic.beta.agents.retrieve(agent_id)
            agent_uuid = derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=str(agent.id))
            _lc_agent_name = agent.name
            _lc_model_id = agent.model.id
            env = await self.runtime.anthropic.beta.environments.retrieve(env_id)
            ma_session = await create_session(
                self.runtime.anthropic,
                agent=agent,
                environment=env,
                mcp_settings=self.runtime.settings.mcp,
                tenant_id=tenant_id,
                account_id=principal.account_id,
                agent_uuid=agent_uuid,
                github_fallback_pat=(
                    self.runtime.settings.github.fallback_pat.get_secret_value()
                    if self.runtime.settings.github.fallback_pat is not None
                    else None
                ),
                github_app_id=self.runtime.settings.github.app_id,
                github_app_private_key=(
                    self.runtime.settings.github.app_private_key.get_secret_value()
                    if self.runtime.settings.github.app_private_key is not None
                    else None
                ),
            )
            ma_session_id = ma_session.id

            async with self.runtime.sessionmaker() as s:
                row = await create_thread_session(
                    s,
                    tenant_id=tenant_id,
                    platform="slack",
                    thread_id=thread_id,
                    account_id=principal.account_id,
                    ma_session_id=ma_session_id,
                )
                await s.commit()
            mapping_id = row.id
            watermark = None
            reused = False
            log.info(
                "slack.session.created",
                session_id=ma_session_id,
                thread_id=thread_id,
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
            ProxyUrlContext(public_url=proxy_base, secret=proxy_secret, team_id=team_id, now=now_i)
            if proxy_base is not None and proxy_secret is not None
            else None
        )

        user_text = (
            content_override if content_override is not None else str(event.get("text") or "")
        )
        author_id = str(event.get("user") or "")
        if not reused:
            # First turn: replay full thread history (capped at 100 messages).
            user_message = await build_context_xml(
                web_client,
                channel=channel,
                thread_ts=thread_id,
                user_query=user_text,
                author_id=author_id,
                proxy=proxy_ctx,
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
                proxy=proxy_ctx,
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

        if proxy_ctx is not None:
            prefix = "\n".join(
                part
                for part in (
                    build_attachment_url_prefix(data_files, proxy_ctx),
                    build_image_url_prefix(inlined, proxy_ctx),
                    build_skipped_image_prefix(images_skipped, proxy_ctx),
                )
                if part
            )
            if prefix:
                user_message = prefix + "\n" + user_message

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

        # --- Run turn ---
        log.info(
            "slack.turn.started",
            thread_id=thread_id,
            session_id=ma_session_id,
            reused=reused,
        )
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
        )
        async with self.runtime.sessionmaker() as s:
            turn_context = await create_slack_turn_context(
                s,
                tenant_id=tenant_id,
                account_id=principal.account_id,
                channel_id=channel,
                thread_ts=thread_id,
                started_at=datetime.now(tz=UTC),
            )
            await s.commit()
        try:
            await run_turn(
                anthropic=self.runtime.anthropic,
                session_id=ma_session_id,
                user_message=user_message,
                lifecycle=lifecycle,
                cancel=cancel_event,
                render_interval_s=2.0,
                image_blocks=image_blocks or None,
            )
        finally:
            # Leak-policy bookkeeping only — a delete failure must not mask the
            # turn's own outcome; stale rows age out via the reader-side TTL.
            with contextlib.suppress(SQLAlchemyError):
                async with self.runtime.sessionmaker() as s:
                    await delete_slack_turn_context(s, id=turn_context.id)
                    await s.commit()

        # --- Watermark ---
        if lifecycle.final_ts is not None:
            async with self.runtime.sessionmaker() as s:
                await update_watermark(s, id=mapping_id, watermark_message_id=lifecycle.final_ts)
                await s.commit()
            log.info(
                "slack.watermark.updated",
                thread_id=thread_id,
                watermark=lifecycle.final_ts,
            )
        else:
            log.info("slack.turn.completed", thread_id=thread_id, session_id=ma_session_id)

    async def drain_and_close(self, client: AsyncBaseSocketModeClient) -> None:
        """Graceful shutdown drain.

        Sets draining=True so new mentions are rejected, polls ``_processing``
        until it empties or the grace window elapses, then closes the
        WebSocket client. Stays within the deployment's 60s kill timeout.
        """
        self.draining = True
        log.info("slack.draining", inflight_threads=len(self._processing))
        deadline = asyncio.get_running_loop().time() + _DRAIN_GRACE_S
        while self._processing and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.5)
        log.info("slack.drain_complete", remaining=len(self._processing))
        await client.close()
