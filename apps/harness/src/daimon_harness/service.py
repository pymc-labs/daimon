"""Authenticated C3 endpoints and a disconnect-independent turn supervisor."""

from __future__ import annotations

import asyncio
import fcntl
import hmac
import json
import logging
import os
import re
import secrets
import sqlite3
import threading
import time
from collections.abc import AsyncIterator, Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Annotated, TypeVar, cast

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import JSONResponse, Response, StreamingResponse
from mux.drivers.selfhosted.wire import (
    ActionRequest,
    CancelResponse,
    EventsResponse,
    EventType,
    HealthResponse,
    InvocationEnvelope,
    TurnAccepted,
    TurnRequest,
    URLSkill,
)
from pydantic import JsonValue, ValidationError
from starlette.middleware.base import RequestResponseEndpoint
from starlette.requests import Request

from .auth import Authenticator
from .checkpoints import Checkpoints, Snapshot, configured_checkpoints, sync_directory
from .codex_adapter import CodexModelAdapter
from .credentials import (
    credential_log_redaction,
    load_bindings,
    process_credentials,
    redact,
    turn_credentials,
)
from .errors import HarnessError
from .isolation import (
    can_switch_uid,
    prepare_service,
    protect_process,
    validate_credential_files,
    validate_credential_sources,
    validate_session_mode,
)
from .journal import MAX_CURSOR, ConflictError, Journal, identifier
from .model_adapter import AzureModelAdapter, ModelAdapter
from .runtime import TurnContext
from .storage_worker import StorageWorker

T = TypeVar("T")

CHECKPOINT_SHUTDOWN_TIMEOUT = 5.0

audit_logger = logging.getLogger("daimon_harness.audit")
audit_logger.setLevel(logging.INFO)
if not audit_logger.handlers:
    audit_logger.addHandler(logging.StreamHandler())
audit_logger.propagate = False


class Supervisor:
    def __init__(
        self,
        home: Path,
        adapter: ModelAdapter,
        session_mode: str = "single",
        token: str = "",
        checkpoints: Checkpoints | None = None,
    ) -> None:
        if checkpoints is not None and session_mode != "single":
            raise RuntimeError("remote checkpoints require single session mode")
        self.checkpoints = checkpoints
        self.restored = False
        self.reopen_pending = False
        self.checkpoint_warned = False
        self.storage_worker = StorageWorker("harness-storage")
        self.wal_worker = StorageWorker("harness-wal")
        self.reservation_worker = StorageWorker("harness-reservations")
        self.checkpoint_task: asyncio.Task[None] | None = None
        self.checkpoint_dirty: str | None = None
        self.pending_snapshot: Snapshot | None = None
        self.checkpoint_pending: str | None = None
        self.operator_credentials = {
            name: value
            for name, value in os.environ.items()
            if name.startswith("HARNESS_MCP_CRED_") and name != "HARNESS_MCP_CRED_BINDINGS"
        }
        self.credential_bindings = load_bindings()
        self.worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="harness-journal")
        self.submissions = asyncio.Lock()
        self.journal = Journal(home)
        self.attach_durability()
        pending = self.journal.root / "checkpoint.pending"
        if pending.exists():
            if checkpoints is None:
                raise RuntimeError("pending checkpoint requires storage configuration")
            self.checkpoint_pending = identifier(pending.read_text())
        self.session_mode = session_mode
        if session_mode == "single":
            try:
                self.journal.configure_single_session()
            except BaseException:
                self.journal.close()
                self.wal_worker.shutdown(wait=False)
                self.reservation_worker.shutdown(wait=False)
                self.storage_worker.shutdown(wait=True)
                self.worker.shutdown(wait=True)
                raise
        self.secrets = process_credentials()
        if token:
            self.secrets["HARNESS_TOKEN"] = token
        self.log_redaction = credential_log_redaction(self.secrets)
        self.log_secrets = self.log_redaction.__enter__()
        self.home = home
        self.adapter = adapter
        self.tasks: dict[str, asyncio.Task[None]] = {}
        self.contexts: dict[str, TurnContext] = {}
        self.changed = asyncio.Event()

    def attach_durability(self) -> None:
        if self.checkpoints is not None:
            self.journal.reserve = self.reserve_sequences
            self.journal.commit_inventory = self.commit_inventory

    def reserve_sequences(self, session_id: str, high_water: int) -> None:
        assert self.checkpoints is not None
        self.reservation_worker.submit(
            self.checkpoints.reserve_sequences, session_id, high_water
        ).result(timeout=15)

    def commit_inventory(self, request: TurnRequest) -> None:
        assert self.checkpoints is not None
        record = self.checkpoints.write_ahead(self.journal, request)
        try:
            self.wal_worker.submit(
                self.checkpoints.upload_write_ahead, request.session_id, request.turn_id, record
            ).result(timeout=15)
        except Exception:
            raise HarnessError("durability_unavailable", "Usage inventory is unavailable") from None

    async def io(self, operation: Callable[..., T], *args: object) -> T:
        # One worker owns all SQLite operations, including ordered event commits.
        future = asyncio.get_running_loop().run_in_executor(self.worker, lambda: operation(*args))
        return await asyncio.shield(future)

    def schedule_checkpoint(self, session_id: str, snapshot: Snapshot | None = None) -> None:
        if self.checkpoints is None:
            return
        self.checkpoint_dirty = session_id
        if self.pending_snapshot is not None:
            self.pending_snapshot.close()
        self.pending_snapshot = snapshot
        if self.checkpoint_task is None or self.checkpoint_task.done():
            self.checkpoint_task = asyncio.create_task(self.checkpoint_loop())

    def retry_pending_checkpoint(self, session_id: str) -> None:
        if self.checkpoint_pending == session_id and (
            self.checkpoint_task is None or self.checkpoint_task.done()
        ):
            self.schedule_checkpoint(session_id)

    async def checkpoint(self, session_id: str) -> None:
        self.schedule_checkpoint(session_id)
        await self.flush_checkpoints()

    async def flush_checkpoints(self) -> None:
        if self.checkpoint_task is not None:
            await asyncio.shield(self.checkpoint_task)

    def checkpoint_result(self, session_id: str, report: dict[str, object] | None) -> None:
        marker = self.journal.root / "checkpoint.pending"
        if report is None:
            self.checkpoint_pending = session_id
            with marker.open("w") as output:
                output.write(session_id)
                output.flush()
                os.fsync(output.fileno())
            self.checkpoint_notice(session_id)
        else:
            self.checkpoint_pending = None
            marker.unlink(missing_ok=True)
            if report.get("degraded"):
                self.checkpoint_notice(session_id)
        sync_directory(self.journal.root)

    async def checkpoint_loop(self) -> None:
        assert self.checkpoints is not None
        while self.checkpoint_dirty is not None:
            session_id, self.checkpoint_dirty = self.checkpoint_dirty, None
            try:
                snapshot, self.pending_snapshot = self.pending_snapshot, None
                if snapshot is None:
                    snapshot = await self.io(self.checkpoints.capture, self.journal, session_id)
                future = asyncio.get_running_loop().run_in_executor(
                    self.storage_worker, self.checkpoints.upload, snapshot
                )
                try:
                    report = await asyncio.wait_for(asyncio.shield(future), timeout=120)
                except TimeoutError:
                    audit_logger.warning("checkpoint_failed stage=upload reason=TimeoutError")
                    await self.io(self.checkpoint_result, session_id, None)
                    # One bounded in-flight attempt: coalesce newer snapshots while
                    # the timed-out operation unwinds, never queue work behind it.
                    await asyncio.shield(future)
                    continue
                await self.io(self.checkpoint_result, session_id, report)
            except Exception as exc:
                audit_logger.warning("checkpoint_failed stage=save reason=%s", type(exc).__name__)
                await self.io(self.checkpoint_result, session_id, None)

    def checkpoint_notice(self, session_id: str) -> None:
        if not self.checkpoint_warned and not any(
            event.type == "checkpoint_warning" for event in self.journal.events(session_id)
        ):
            # Uploads can finish after the completed turn's reader has closed.
            # Keep the notice durable until a new turn can display it.
            marker = self.journal.root / "checkpoint.notice"
            with marker.open("w") as output:
                output.write(session_id)
                output.flush()
                os.fsync(output.fileno())
            sync_directory(self.journal.root)

    def deliver_checkpoint_notice(self, request: TurnRequest) -> None:
        marker = self.journal.root / "checkpoint.notice"
        if not marker.exists():
            return
        if marker.read_text() != request.session_id:
            raise ConflictError("session_bound")
        if not any(
            event.type == "checkpoint_warning" for event in self.journal.events(request.session_id)
        ):
            self.journal.append(
                request,
                "checkpoint_warning",
                {"message": "Recent messages and files may be lost after a restart."},
            )
        self.checkpoint_warned = True
        marker.unlink()
        sync_directory(self.journal.root)

    def durable_append(
        self, request: TurnRequest, kind: EventType, data: dict[str, JsonValue]
    ) -> Snapshot | None:
        self.journal.append(request, kind, data)
        if self.checkpoints is not None and kind == "turn_ended":
            try:
                return self.checkpoints.capture(self.journal, request.session_id)
            except Exception as exc:
                audit_logger.warning(
                    "checkpoint_failed stage=capture reason=%s", type(exc).__name__
                )
                self.checkpoint_result(request.session_id, None)
        return None

    async def cleanup_failed_turns(self) -> None:
        for turn_id, context in list(self.contexts.items()):
            task = self.tasks.get(turn_id)
            if task is not None and not task.done():
                continue
            try:
                await context.stop_workers()
            except Exception:
                raise HarnessError(
                    "workers_active", "Tool cleanup incomplete; session sealed"
                ) from None
            self.contexts.pop(turn_id, None)

    def reopen_journal(self) -> None:
        if self.reopen_pending:
            diverged = self.journal.diverged
            self.journal = Journal(self.home)
            self.journal.diverged = diverged
            self.attach_durability()
            self.reopen_pending = False

    def prepare_session(self, session_id: str, after_seq: int = 0) -> None:
        try:
            self.reopen_journal()
        except (sqlite3.Error, OSError):
            raise HarnessError("durability_unavailable", "Local journal is unavailable") from None
        if self.journal.diverged:
            self.restored = False
        try:
            if self.session_mode == "single":
                self.journal.bind_session(session_id)
            after_seq = self.journal.record_cursor(session_id, after_seq)
        except sqlite3.Error:
            raise HarnessError("durability_unavailable", "Local journal is unavailable") from None
        if self.checkpoints is not None and not self.restored:
            try:
                self.journal = self.checkpoints.restore(self.journal, session_id, after_seq)
                self.attach_durability()
                # Clear the seal only after all original remote events and the
                # reservation fence have committed locally. A failed merge keeps it.
                self.journal.diverged = False
                self.journal.flush_inventory(session_id)
                self.journal.bind_session(session_id)
                cursor_file = self.journal.root / "restore.cursor"
                if cursor_file.exists():
                    saved = json.loads(cursor_file.read_bytes())
                    if saved["session_id"] != session_id:
                        raise ConflictError("session_bound")
                    self.journal.record_cursor(session_id, max(after_seq, saved["after_seq"]))
                # WAL reconciliation is complete. If repair fails, retry it
                # against this incarnation instead of abandoning its new block.
                self.restored = True
                self.recover(sandbox_lost=cursor_file.exists())
            except ConflictError:
                raise
            except Exception as exc:
                audit_logger.warning(
                    "checkpoint_failed stage=restore reason=%s", type(exc).__name__
                )
                self.reopen_pending = True
                self.journal.close()
                try:
                    self.reopen_journal()
                    self.journal.record_cursor(session_id, after_seq)
                except (sqlite3.Error, OSError):
                    raise HarnessError(
                        "durability_unavailable", "Local journal is unavailable"
                    ) from None
                if isinstance(exc, HarnessError):
                    raise exc
                raise HTTPException(status_code=503, detail="Session restore unavailable") from None
            self.restored = True
        self.journal.flush_inventory(session_id)
        if any(
            not self.journal.ended(request.turn_id)
            and (request.turn_id not in self.tasks or self.tasks[request.turn_id].done())
            for request in self.journal.turns(session_id)
        ):
            self.recover(sandbox_lost=(self.journal.root / "restore.cursor").exists())
        self.journal.announce_gap(session_id)

    async def ensure_session(self, session_id: str, after_seq: int = 0) -> None:
        identifier(session_id)
        async with self.submissions:
            try:
                await self.cleanup_failed_turns()
                await self.io(self.prepare_session, session_id, after_seq)
                self.retry_pending_checkpoint(session_id)
            except ConflictError:
                raise HTTPException(status_code=409, detail="session_bound") from None
            except ValueError:
                raise HTTPException(status_code=422, detail="Invalid client cursor") from None

    def recover(self, sandbox_lost: bool = False) -> None:
        self.journal.require_converged()
        if (self.journal.root / "restore.pending").exists() or (
            (self.journal.root / "restore.cursor").exists() and not sandbox_lost
        ):
            # Partial publication must be restored before repairing turn state.
            return
        cursor_file = self.journal.root / "restore.cursor"
        sandbox_lost = sandbox_lost or cursor_file.exists()
        # A claimed turn can have executed tools. Never retry it after a process crash.
        for request in self.journal.turns():
            if not self.journal.ended(request.turn_id):
                task = self.tasks.get(request.turn_id)
                if task is not None and not task.done():
                    continue
                if request.turn_id in self.contexts:
                    # Task completion is not proof of worker termination (I5).
                    raise HarnessError("workers_active", "Tool cleanup incomplete; session sealed")
                if not any(
                    event.turn_id == request.turn_id
                    and event.type == "error"
                    and event.data.get("kind") in {"sandbox_lost", "process_interrupted"}
                    for event in self.journal.events(request.session_id, include_pending=True)
                ):
                    self.journal.append(
                        request,
                        "error",
                        {
                            "kind": "sandbox_lost" if sandbox_lost else "process_interrupted",
                            "message": "Harness restarted during the turn",
                            "retryable": False,
                        },
                    )
                for request_id in self.journal.pending_requests(request):
                    self.journal.append(
                        request,
                        "error",
                        {
                            "kind": "usage_unavailable",
                            "message": "Restart left model usage uncertain",
                            "retryable": False,
                            "request_id": request_id,
                        },
                    )
                self.journal.append(
                    request,
                    "turn_ended",
                    {
                        "outcome": "interrupted",
                        "error": {
                            "kind": "sandbox_lost" if sandbox_lost else "process_interrupted",
                            "message": "Harness restarted during the turn",
                        },
                    },
                )

        cursor_file.unlink(missing_ok=True)
        sync_directory(self.journal.root)

    async def write_ahead(self, request: TurnRequest) -> None:
        if self.checkpoints is None:
            return
        try:
            record = await self.io(self.checkpoints.write_ahead, self.journal, request)
            async with asyncio.timeout(15):
                await asyncio.get_running_loop().run_in_executor(
                    self.wal_worker,
                    self.checkpoints.upload_write_ahead,
                    request.session_id,
                    request.turn_id,
                    record,
                )
        except Exception:
            raise HarnessError(
                "durability_unavailable", "Remote write-ahead is unavailable"
            ) from None

    async def run(self, request: TurnRequest, credentials: dict[str, str]) -> None:
        credentials = self.secrets | credentials
        usage_count = 0
        failure: dict[str, JsonValue] | None = None

        async def emit(kind: EventType, data: dict[str, JsonValue]) -> None:
            nonlocal usage_count, failure
            if kind == "usage":
                usage_count += 1
            if kind == "error" and data.get("kind") != "checkpoint_warning":
                failure = {"kind": data["kind"], "message": data["message"]}
            snapshot = await self.io(
                self.durable_append,
                request,
                kind,
                cast(dict[str, JsonValue], redact(data, credentials)),
            )
            if kind == "turn_ended":
                self.schedule_checkpoint(request.session_id, snapshot)
            self.changed.set()

        outcome = "completed"
        context: TurnContext | None = None
        workers_stopped = False
        try:
            await self.write_ahead(request)
            await self.io(self.deliver_checkpoint_notice, request)
            sandbox, uid = await self.io(self.journal.sandbox, request.session_id)
            context = TurnContext(
                request,
                sandbox,
                emit,
                credentials=credentials,
                credential_bindings=self.credential_bindings,
                tool_uid=uid,
                state_dir=self.journal.root / "sessions" / request.session_id,
            )
            self.contexts[request.turn_id] = context
            await emit("status_running", {})
            previous = await self.io(self.journal.history, request.session_id)
            await self.io(
                self.journal.save_history,
                request.session_id,
                cast(
                    list[JsonValue],
                    redact(
                        previous
                        + [
                            {
                                "role": "user",
                                "content": "Interrupted turn input: "
                                + request.model_dump_json(include={"input"}),
                            }
                        ],
                        credentials,
                    ),
                ),
            )
            async with asyncio.timeout(request.limits.deadline_s):
                history = await self.adapter.run(context, previous)
            if not usage_count:
                raise HarnessError("usage_missing", "Turn completed without measured usage")
            await self.io(
                self.journal.save_history,
                request.session_id,
                cast(list[JsonValue], redact(history, credentials)),
            )
        except (asyncio.CancelledError, TimeoutError):
            outcome = "interrupted"
        except Exception as exc:
            outcome = "errored"
            # SDK exception messages can include credentials and request bodies.
            kind = exc.kind if isinstance(exc, HarnessError) else type(exc).__name__
            if isinstance(exc, HarnessError):
                failure = {"kind": kind, "message": str(exc)}
            if failure is None:
                await emit(
                    "error",
                    {
                        "kind": kind,
                        "message": "Harness turn failed",
                        "retryable": kind == "durability_unavailable",
                    },
                )
        finally:

            async def finalize() -> None:
                nonlocal failure, workers_stopped
                if context is not None:
                    await context.stop_workers()
                    workers_stopped = True
                original_failure = failure
                for request_id in await self.io(self.journal.pending_requests, request):
                    await emit(
                        "error",
                        {
                            "kind": "usage_unavailable",
                            "message": "Model request ended without usage",
                            "retryable": False,
                            "request_id": request_id,
                        },
                    )
                if original_failure is not None:
                    failure = original_failure
                data: dict[str, JsonValue] = {"outcome": outcome}
                if outcome == "errored":
                    error = failure or {"kind": "harness", "message": "Harness turn failed"}
                    if error.get("kind") == "durability_unavailable":
                        error["retryable"] = True
                    data["error"] = error
                try:
                    await emit("turn_ended", data)
                finally:
                    credentials.clear()
                    self.changed.set()

            finalization = asyncio.create_task(finalize())
            try:
                while not finalization.done():
                    try:
                        await asyncio.shield(finalization)
                    except asyncio.CancelledError:
                        outcome = "interrupted"
                await finalization
            finally:
                credentials.clear()
                # Retain the context until worker cleanup succeeds (I5).
                if workers_stopped:
                    self.contexts.pop(request.turn_id, None)
                self.changed.set()

    async def submit(
        self, request: TurnRequest, mcp_authorization: str | None = None, after_seq: int = 0
    ) -> None:
        async with self.submissions:
            await self.cleanup_failed_turns()
            await self.io(self.prepare_session, request.session_id, after_seq)
            self.retry_pending_checkpoint(request.session_id)
            if self.session_mode == "single":
                await self.io(self.journal.bind_session, request.session_id)
            if await self.io(self.journal.is_replay, request):
                return
            if any(isinstance(skill, URLSkill) for skill in request.skills):
                raise ValueError("URL skills are not supported; send an inline bundle")
            credentials = turn_credentials(
                request, mcp_authorization, self.operator_credentials, self.credential_bindings
            )
            self.tasks = {key: task for key, task in self.tasks.items() if not task.done()}
            await self.io(self.journal.sandbox, request.session_id)
            if await self.io(self.journal.claim, request):
                self.tasks[request.turn_id] = asyncio.create_task(self.run(request, credentials))

    async def stream(self, request: TurnRequest) -> AsyncIterator[str]:
        after = 0
        while True:
            self.changed.clear()
            await self.ensure_session(request.session_id)
            complete = await self.io(self.journal.ended, request.turn_id)
            events = await self.io(self.journal.events, request.session_id, after)
            if not events and not complete:
                try:
                    await asyncio.wait_for(self.changed.wait(), timeout=15)
                except TimeoutError:
                    yield ": keepalive\n\n"
                continue
            for event in events:
                after = event.seq
                if event.turn_id == request.turn_id:
                    yield (
                        f"id: {event.seq}\nevent: {event.type}\ndata: {event.model_dump_json()}\n\n"
                    )
            if complete:
                return

    async def close(self) -> None:
        for task in self.tasks.values():
            if not task.done() and not task.cancelling():
                task.cancel()
        await asyncio.gather(*self.tasks.values(), return_exceptions=True)

        def close_resources() -> None:
            if self.checkpoints is not None:
                self.checkpoints.close()
            if isinstance(self.adapter, AzureModelAdapter) and self.adapter.identity is not None:
                self.adapter.identity.close()

        close_submitted = False
        try:
            async with asyncio.timeout(CHECKPOINT_SHUTDOWN_TIMEOUT):
                await self.flush_checkpoints()
                if not self.wal_worker.busy and not self.reservation_worker.busy:
                    closing = asyncio.get_running_loop().run_in_executor(
                        self.storage_worker, close_resources
                    )
                    close_submitted = True
                    await closing
        except Exception as exc:
            audit_logger.warning("checkpoint_failed stage=shutdown reason=%s", type(exc).__name__)
            if self.checkpoint_task is not None:
                self.checkpoint_task.cancel()
                await asyncio.gather(self.checkpoint_task, return_exceptions=True)
        if self.pending_snapshot is not None:
            self.pending_snapshot.close()
            self.pending_snapshot = None
        await self.io(self.journal.close)
        self.wal_worker.shutdown(wait=False, cancel_futures=True)
        self.reservation_worker.shutdown(wait=False, cancel_futures=True)
        self.storage_worker.shutdown(wait=False, cancel_futures=True)
        self.worker.shutdown(wait=True)

        def release_storage() -> None:
            # Keep redaction active while an abandoned SDK call can still log.
            # Cleanup is also daemonized: closing a broken network client must
            # not become a second unbounded shutdown wait.
            for worker in (self.storage_worker, self.wal_worker, self.reservation_worker):
                if worker.active is not None:
                    with suppress(BaseException):
                        worker.active.result()
            try:
                if not close_submitted:
                    close_resources()
            except Exception as exc:
                audit_logger.warning(
                    "checkpoint_failed stage=resource_close reason=%s", type(exc).__name__
                )
            finally:
                self.log_redaction.__exit__(None, None, None)
                self.secrets.clear()

        if (
            self.storage_worker.busy
            or self.wal_worker.busy
            or (self.checkpoints is not None and not close_submitted)
        ):
            threading.Thread(
                target=release_storage, name="harness-storage-close", daemon=True
            ).start()
        else:
            release_storage()


def create_app(
    *,
    home: Path | None = None,
    token: str | None = None,
    adapter: ModelAdapter | None = None,
    checkpoints: Checkpoints | None = None,
) -> FastAPI:
    """One worker per session sandbox; the HOME lease rejects accidental replicas."""
    root = home or Path.home()
    validate_credential_sources()
    test_mode = os.environ.get("HARNESS_TEST_MODE") == "1"
    if test_mode:
        forbidden = {
            name
            for name in os.environ
            if (
                name
                in {
                    "HARNESS_TOKEN",
                    "HARNESS_CHECKPOINT_TOKEN",
                    "AZURE_OPENAI_API_KEY",
                    "OPENAI_API_KEY",
                    "AZURE_CLIENT_SECRET",
                    "AZURE_CLIENT_CERTIFICATE_PATH",
                    "IDENTITY_HEADER",
                    "MSI_SECRET",
                }
                or (name.startswith("HARNESS_MCP_CRED_") and name != "HARNESS_MCP_CRED_BINDINGS")
            )
            and os.environ.get(name)
        }
        if adapter is None or isinstance(adapter, AzureModelAdapter) or forbidden:
            raise RuntimeError("test mode requires a fake adapter and no configured credentials")
    elif not can_switch_uid():
        raise RuntimeError("production requires separate service and tool UIDs")
    if not test_mode:
        validate_credential_files(home=root)
    pod_mode = os.environ.get("HARNESS_ROLE") == "sandbox"
    auth = Authenticator(token or (secrets.token_urlsafe(32) if pod_mode else None))
    session_mode = validate_session_mode()
    loop = os.environ.get("HARNESS_LOOP", "agents_sdk")
    if loop not in {"agents_sdk", "codex"}:
        raise RuntimeError("HARNESS_LOOP must be agents_sdk or codex")
    selected = adapter or (CodexModelAdapter() if loop == "codex" else AzureModelAdapter())
    audit_logger.info(
        json.dumps(
            {
                "event": "harness.model_provider",
                "loop": loop,
                "provider": "openai_probe" if getattr(selected, "live_probe", "") else "azure",
            }
        )
    )
    if (
        checkpoints is None
        and test_mode
        and any(
            os.environ.get(name)
            for name in ("HARNESS_CHECKPOINT_ACCOUNT_URL", "HARNESS_CHECKPOINT_CONTAINER")
        )
    ):
        raise RuntimeError("test mode requires an injected checkpoint store")
    configured = checkpoints or (
        None
        if pod_mode
        else configured_checkpoints(
            selected.identity
            if isinstance(selected, AzureModelAdapter) and not selected.live_probe
            else None
        )
    )
    supervisor = Supervisor(root, selected, session_mode, auth.token, configured)
    if not test_mode:
        prepare_service(root)
        protect_process()
    audit_logger.info(json.dumps({"event": "harness.session_mode", "mode": session_mode}))

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        with (supervisor.journal.root / "process.lock").open("a") as lease:
            fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if supervisor.checkpoints is None:
                await supervisor.io(supervisor.recover)
            try:
                yield
            finally:
                await supervisor.close()
                fcntl.flock(lease, fcntl.LOCK_UN)

    async def authenticate(
        authorization: Annotated[str | None, Header()] = None,
        x_agent_user_id: Annotated[str | None, Header()] = None,
        x_agent_foundry_call_id: Annotated[str | None, Header()] = None,
    ) -> str:
        return await auth.verify(authorization, x_agent_user_id, x_agent_foundry_call_id)

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    secured = [Depends(authenticate)]
    app.state.supervisor = supervisor

    bootstrapped = False
    last_activity = time.time()

    @app.middleware("http")
    async def activity(request: Request, call_next: RequestResponseEndpoint) -> Response:
        nonlocal last_activity
        response = await call_next(request)
        if request.url.path.startswith("/v0/") and response.status_code < 400:
            last_activity = time.time()
        return response

    if pod_mode:
        # TLS requires a controller client certificate before HTTP is accepted.
        @app.post("/internal/bootstrap")
        async def bootstrap(request: Request):
            nonlocal bootstrapped
            body = await request.json()
            if bootstrapped:
                if not hmac.compare_digest(auth.token, body["c3"]):
                    raise HTTPException(409, "already bootstrapped")
                return {"ok": True}
            from .pvc_store import ControllerStore

            supervisor.checkpoints = Checkpoints(
                ControllerStore(os.environ["HARNESS_CHECKPOINT_CONTROLLER_URL"], body["checkpoint"])
            )
            supervisor.attach_durability()
            auth.token = body["c3"]
            supervisor.secrets["HARNESS_TOKEN"] = body["c3"]
            supervisor.secrets["HARNESS_CHECKPOINT_TOKEN"] = body["checkpoint"]
            supervisor.log_secrets.update(supervisor.secrets)
            bootstrapped = True
            return {"ok": True}

        app.state.bootstrap_handler = bootstrap

        @app.get("/internal/activity", dependencies=secured)
        async def pod_activity():
            return {
                "active": any(not task.done() for task in supervisor.tasks.values()),
                "last_activity": last_activity,
            }

        app.state.pod_activity_handler = pod_activity

    async def health() -> HealthResponse:
        return HealthResponse(version=os.environ.get("HARNESS_VERSION", "development"))

    async def turns(
        request: TurnRequest,
        caller: str = Depends(authenticate),
        accept: Annotated[str | None, Header()] = None,
        x_daimon_mcp_authorization: Annotated[str | None, Header()] = None,
        session_id: Annotated[str | None, Query()] = None,
        after_seq: int = Query(default=0, ge=0, le=MAX_CURSOR),
    ) -> Response:
        if session_id is not None and session_id != request.session_id:
            raise HTTPException(status_code=409, detail="session_mismatch")
        audit_logger.info(
            json.dumps(
                {
                    "event": "harness.turn_authorized",
                    "caller": caller,
                    "session_id": request.session_id,
                    "turn_id": request.turn_id,
                }
            )
        )
        try:
            await supervisor.submit(request, x_daimon_mcp_authorization, after_seq)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="Invalid turn configuration") from exc
        except ConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if accept and "application/json" in accept and "text/event-stream" not in accept:
            return JSONResponse(TurnAccepted(turn_id=request.turn_id).model_dump(), status_code=202)
        return StreamingResponse(
            supervisor.stream(request),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    async def durability_error(_request: Request, exc: Exception) -> JSONResponse:
        assert isinstance(exc, HarnessError)
        return JSONResponse(
            status_code=503,
            content={"detail": {"kind": exc.kind, "message": str(exc), "retryable": True}},
        )

    app.add_exception_handler(HarnessError, durability_error)

    async def restore_for_turn(turn_id: str, session_id: str | None, after_seq: int) -> None:
        if supervisor.checkpoints is not None and session_id is None:
            raise HTTPException(status_code=422, detail="session_id query parameter is required")
        if session_id is None:
            request = await supervisor.io(supervisor.journal.find, turn_id)
            if request is not None:
                session_id = request.session_id
        if session_id is not None:
            try:
                await supervisor.ensure_session(session_id, after_seq)
            except ValueError:
                raise HTTPException(status_code=422, detail="Invalid session identifier") from None
            request = await supervisor.io(supervisor.journal.find, turn_id)
            if request is not None and request.session_id != session_id:
                raise HTTPException(status_code=409, detail="session_mismatch")

    async def actions(
        turn_id: str,
        action: ActionRequest,
        session_id: Annotated[str | None, Query()] = None,
        after_seq: int = Query(default=0, ge=0, le=MAX_CURSOR),
    ) -> dict[str, bool]:
        await restore_for_turn(turn_id, session_id, after_seq)
        context = supervisor.contexts.get(turn_id)
        if context is None:
            raise HTTPException(status_code=404, detail="No active turn")
        prior = context.decisions.get(action.action_id)
        if prior is not None:
            if prior != action:
                raise HTTPException(status_code=409, detail="Action already decided differently")
            return {"accepted": True}
        future = context.pending.get(action.action_id)
        if future is None or future.done():
            raise HTTPException(status_code=404, detail="No pending action")
        context.decisions[action.action_id] = action
        future.set_result(action)
        return {"accepted": True}

    async def cancel(
        turn_id: str,
        session_id: Annotated[str | None, Query()] = None,
        after_seq: int = Query(default=0, ge=0, le=MAX_CURSOR),
    ) -> CancelResponse:
        await restore_for_turn(turn_id, session_id, after_seq)
        task = supervisor.tasks.get(turn_id)
        if task is None or task.done():
            return CancelResponse(cancelled=False)
        # Ensure a newly submitted task reaches its finally block before cancellation.
        await asyncio.sleep(0)
        if task.done():
            return CancelResponse(cancelled=False)
        if not task.cancelling():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        if not await supervisor.io(supervisor.journal.ended, turn_id):
            raise HTTPException(status_code=503, detail="Tool cleanup incomplete; session sealed")
        return CancelResponse(cancelled=True)

    async def events(
        session_id: str,
        after_seq: int = Query(default=0, ge=0, le=MAX_CURSOR),
        context_session_id: Annotated[str | None, Query(alias="session_id")] = None,
        replay_after_seq: Annotated[int | None, Query(ge=0, le=MAX_CURSOR)] = None,
    ) -> EventsResponse:
        if context_session_id is not None and context_session_id != session_id:
            raise HTTPException(status_code=409, detail="session_mismatch")
        try:
            identifier(session_id)
            await supervisor.ensure_session(session_id, after_seq)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="Invalid session identifier") from exc
        if supervisor.session_mode == "single":
            try:
                await supervisor.io(supervisor.journal.bind_session, session_id)
            except ConflictError as exc:
                raise HTTPException(status_code=409, detail="session_bound") from exc
        complete = await supervisor.io(supervisor.journal.complete, session_id)
        return EventsResponse(
            events=await supervisor.io(
                supervisor.journal.events,
                session_id,
                after_seq if replay_after_seq is None else min(after_seq, replay_after_seq),
            ),
            complete=complete,
        )

    async def invoke(
        envelope: InvocationEnvelope,
        caller: str = Depends(authenticate),
        accept: Annotated[str | None, Header()] = None,
        x_daimon_mcp_authorization: Annotated[str | None, Header()] = None,
        session_id: Annotated[str | None, Query()] = None,
        after_seq: int = Query(default=0, ge=0, le=MAX_CURSOR),
    ) -> Response:
        route = envelope.c3
        method, path = route.method, route.path
        context_id = route.query.get("session_id", session_id)
        if context_id is not None and not isinstance(context_id, str):
            raise HTTPException(status_code=422, detail="Invalid session context")
        if session_id is not None and context_id != session_id:
            raise HTTPException(status_code=409, detail="session_mismatch")
        if set(route.query) - {"session_id", "after_seq"} and not (
            method == "GET" and re.fullmatch(r"/v0/sessions/[^/]+/events", path)
        ):
            raise HTTPException(status_code=422, detail="Unexpected query parameters")
        try:
            raw_cursor = route.query.get("after_seq", after_seq)
            if isinstance(raw_cursor, bool) or not isinstance(raw_cursor, (str, int)):
                raise ValueError("invalid cursor")
            cursor = int(raw_cursor)
            if not 0 <= cursor <= MAX_CURSOR:
                raise ValueError("negative cursor")
        except ValueError:
            raise HTTPException(status_code=422, detail="Invalid cursor") from None
        if method == "GET" and route.body is not None:
            raise HTTPException(status_code=422, detail="GET body must be null")
        try:
            if method == "GET" and path == "/v0/healthz":
                return JSONResponse((await health()).model_dump())
            if method == "POST" and path == "/v0/turns":
                return await turns(
                    TurnRequest.model_validate(route.body),
                    caller,
                    accept,
                    x_daimon_mcp_authorization,
                    context_id,
                    cursor,
                )
            if method == "POST" and (
                match := re.fullmatch(r"/v0/turns/([^/]+)/(actions|cancel)", path)
            ):
                turn_id, operation = match.groups()
                if operation == "actions":
                    return JSONResponse(
                        await actions(
                            turn_id, ActionRequest.model_validate(route.body), context_id, cursor
                        )
                    )
                if route.body not in (None, {}):
                    raise HTTPException(status_code=422, detail="Cancel body must be empty")
                return JSONResponse((await cancel(turn_id, context_id, cursor)).model_dump())
            if method == "GET" and (match := re.fullmatch(r"/v0/sessions/([^/]+)/events", path)):
                if set(route.query) - {"after_seq", "session_id", "replay_after_seq"}:
                    raise HTTPException(status_code=422, detail="Unexpected query parameters")
                raw_after = route.query.get("after_seq", 0)
                if isinstance(raw_after, bool) or not isinstance(raw_after, (str, int)):
                    raise ValueError("invalid cursor")
                after = int(raw_after)
                if not 0 <= after <= MAX_CURSOR:
                    raise ValueError("negative cursor")
                replay = route.query.get("replay_after_seq")
                if replay is not None:
                    if isinstance(replay, bool) or not isinstance(replay, (str, int)):
                        raise ValueError("invalid replay cursor")
                    replay = int(replay)
                    if not 0 <= replay <= MAX_CURSOR:
                        raise ValueError("invalid replay cursor")
                return JSONResponse(
                    (await events(match[1], after, context_id, replay)).model_dump(mode="json")
                )
        except (ValidationError, ValueError) as exc:
            raise HTTPException(status_code=422, detail="Invalid C3 invocation") from exc
        raise HTTPException(status_code=404, detail="Unknown C3 route")

    async def readiness() -> dict[str, bool]:
        return {"ready": True}

    app.add_api_route("/v0/healthz", health, methods=["GET"], dependencies=secured)
    app.add_api_route("/v0/turns", turns, methods=["POST"], dependencies=secured)
    app.add_api_route(
        "/v0/turns/{turn_id}/actions", actions, methods=["POST"], dependencies=secured
    )
    app.add_api_route("/v0/turns/{turn_id}/cancel", cancel, methods=["POST"], dependencies=secured)
    app.add_api_route(
        "/v0/sessions/{session_id}/events", events, methods=["GET"], dependencies=secured
    )
    app.add_api_route("/invocations", invoke, methods=["POST"], dependencies=secured)
    app.add_api_route("/readiness", readiness, methods=["GET"])
    app.state.activity_handler = activity
    return app
