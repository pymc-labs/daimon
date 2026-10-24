"""Scoped, read-only fleet capture and restricted atomic evidence publication.

Persistence facts are not native/platform observations or a scenario verdict.
Missing owner joins stay PENDING. No provider client or financial mutation runs.
"""

from __future__ import annotations

import argparse
import asyncio
import ctypes
import hashlib
import json
import os
import shutil
import stat
import tempfile
from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Literal, NoReturn, Protocol
from uuid import UUID

from daimon.core.fleet import FleetConflict, FleetRun, FleetWorker, check_result, worker_slot
from daimon.core.stores import fleet, mux_state
from daimon.testing.outcome_oracle import (
    EvidenceModel,
    FleetEvidence,
    FleetResultFacts,
    FleetWorkerCapture,
)
from mux.contracts.events import (
    AgentMessagePayload,
    Event,
    TextPart,
    ToolResultPayload,
    UserMessagePayload,
)
from mux.contracts.ids import ChannelRef, ThreadRef
from mux.contracts.resources import ProjectionSnapshot, ProviderBinding
from mux.state.lease import Slot
from mux.state.store import binding_slot
from mux.testing.conformance.recording import Audit, RecordingError, normalized_event, string_fields
from pydantic import Field, JsonValue, ValidationError, model_validator
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

MAX_BYTES = 8 * 1024 * 1024
MAX_EVENTS = 10000
FILES = ("export.json", "manifest.json", "SHA256SUMS")


class ExportRefused(Exception):
    """Constant, content-free refusal: never print DB or payload errors."""


def refuse() -> NoReturn:
    raise ExportRefused("fleet evidence export refused")


def export_event(event: Event, *, project: bool) -> Event:
    normalized = normalized_event(event, project=project)
    payload = normalized.typed_payload()
    if isinstance(payload, (AgentMessagePayload, UserMessagePayload, ToolResultPayload)) and any(
        not isinstance(part, TextPart) for part in payload.content
    ):
        raise RecordingError("non-text content is outside fleet export scope")
    return normalized


class Gap(EvidenceModel):
    code: Literal[
        "BINDING_UNAVAILABLE",
        "PROJECTION_UNAVAILABLE",
        "PAYLOAD_OMITTED",
        "NATIVE_PLATFORM_CAPTURE_UNBOUND",
        "WAKE_READER_UNBOUND",
        "OUTCOME_READER_UNBOUND",
        "FINANCIAL_EXECUTION_JOIN_UNBOUND",
        "ROOT_INPUT_JOIN_UNBOUND",
    ]
    owner: Literal["N3", "N4", "N5", "N8", "N9"]
    worker_id: UUID | None = None


class JournalRecord(EvidenceModel):
    """Original fixed normalized event, or explicit metadata-only omission."""

    session_id: str = Field(min_length=1)
    sequence: int = Field(ge=0)
    event_id: str = Field(min_length=1)
    event_type: str = Field(min_length=1)
    authority: Literal["record", "preview", "reconciled", "gap"]
    event: Event | None = None
    payload_omitted: bool = False

    @model_validator(mode="after")
    def valid_event(self) -> JournalRecord:
        if self.event is None:
            if not self.payload_omitted:
                refuse()
        elif self.payload_omitted or (
            self.event.session_id,
            self.event.sequence,
            self.event.id,
            self.event.type,
            self.event.authority,
        ) != (self.session_id, self.sequence, self.event_id, self.event_type, self.authority):
            refuse()
        else:
            export_event(self.event, project=False)
        return self


class JournalCapture(EvidenceModel):
    worker_id: UUID | None = None
    binding: ProviderBinding
    projection_cursor: str | None = None
    records: tuple[JournalRecord, ...]
    metadata_complete: bool

    @model_validator(mode="after")
    def closed_binding(self) -> JournalCapture:
        session_id = self.binding.native_refs.get("session")
        if not session_id or set(self.binding.native_refs) != {"session"}:
            refuse()
        sequences = [record.sequence for record in self.records]
        if sequences != sorted(set(sequences)) or any(
            record.session_id != session_id
            or (record.event is not None and record.event.native.provider != self.binding.provider)
            for record in self.records
        ):
            refuse()
        return self


class WorkerPersistence(EvidenceModel):
    """Other original row facts, independent of native or billable actual spend."""

    worker_id: UUID
    version: int = Field(ge=0)
    budget_usd: Decimal = Field(ge=0)
    reserved_usd: Decimal = Field(ge=0)
    deadline_at: datetime
    max_turns: int = Field(ge=1)
    error_code: str | None


class FleetExportV1(EvidenceModel):
    schema_version: Literal[1] = 1
    tenant_id: UUID
    run_id: UUID
    attempt_id: str = Field(min_length=1, max_length=128, pattern=r"^[a-zA-Z0-9_.-]+$")
    source_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    started_at: datetime
    captured_at: datetime
    consistency: Literal["repeatable_read", "source_rechecked"]
    fleet: FleetEvidence
    journals: tuple[JournalCapture, ...]
    worker_rows: tuple[WorkerPersistence, ...]
    gaps: tuple[Gap, ...]

    @model_validator(mode="after")
    def exact_scope(self) -> FleetExportV1:
        run = self.fleet.run
        if run is None or (run.tenant_id, run.id) != (self.tenant_id, self.run_id):
            refuse()
        if any(t.tzinfo is None for t in (self.started_at, self.captured_at)) or (
            self.captured_at < self.started_at
        ):
            refuse()
        workers = self.fleet.workers
        if len({w.worker.id for w in workers}) != len(workers) or len(
            {w.worker.ordinal for w in workers}
        ) != len(workers):
            refuse()
        if {w.worker_id for w in self.worker_rows} != {w.worker.id for w in workers} or (
            len(self.worker_rows) != len(workers)
        ):
            refuse()
        worker_by_id = {capture.worker.id: capture for capture in workers}
        for row in self.worker_rows:
            worker = worker_by_id[row.worker_id].worker
            if row.deadline_at.tzinfo is None or (row.version, row.error_code) != (
                worker.version,
                worker.error_code,
            ):
                refuse()
        for capture in workers:
            worker = capture.worker
            # The old full worker DTO must never cross the persistence boundary.
            if isinstance(worker, FleetWorker):
                refuse()
            if (worker.tenant_id, worker.run_id) != (self.tenant_id, self.run_id) or (
                capture.attempt_id,
                capture.run_id,
                capture.root_session_id,
            ) != (self.attempt_id, str(run.id), run.orchestrator_session_id):
                refuse()
            if (
                capture.input_record is not None
                or capture.terminal is not None
                or any(
                    value is not None
                    for value in (
                        capture.native_backend,
                        capture.native_model,
                        capture.native_started_s,
                    )
                )
            ):
                refuse()  # This core does not manufacture or ingest live sidecars.
            if capture.result is not None:
                export_event(capture.result, project=False)
                if worker.result is None:
                    refuse()
                check_result(run, worker, capture.binding, capture.result, worker.result)
        if self.fleet.wakes or self.fleet.collect is not None or self.fleet.wakes_capture_complete:
            refuse()  # Owner readers are added in their separate consumer slices.
        seen: set[UUID | None] = set()
        sessions: set[str] = set()
        for journal in self.journals:
            if journal.worker_id in seen:
                refuse()
            seen.add(journal.worker_id)
            binding = journal.binding
            session_id = binding.native_refs["session"]
            if session_id in sessions:
                refuse()
            sessions.add(session_id)
            if journal.worker_id is None:
                expected_slot = root_slot(run)
                if journal.records or journal.metadata_complete:
                    refuse()
                if (binding.id, session_id) != (
                    run.orchestrator_binding_id,
                    run.orchestrator_session_id,
                ):
                    refuse()
            else:
                capture = worker_by_id.get(journal.worker_id)
                if capture is None:
                    refuse()
                expected_slot = worker_slot(run, capture.worker)
                if binding != capture.binding or (str(binding.provider), binding.profile) != (
                    capture.worker.backend,
                    capture.worker.profile,
                ):
                    refuse()
                if capture.worker.result is not None:
                    matches = [
                        r.event
                        for r in journal.records
                        if r.sequence == (capture.worker.result.sequence)
                    ]
                    if matches != [capture.result] or capture.result is None:
                        refuse()
            if binding_slot(binding) != expected_slot:
                refuse()
        for capture in workers:
            if capture.worker.result is not None and (
                capture.binding is None or capture.result is None
            ):
                refuse()
            if capture.binding is not None and capture.worker.id not in seen:
                refuse()
        if None not in seen and not any(
            gap.code == "BINDING_UNAVAILABLE" and gap.worker_id is None for gap in self.gaps
        ):
            refuse()
        return self


class ExportManifest(EvidenceModel):
    schema_version: Literal[1] = 1
    tenant_id: UUID
    run_id: UUID
    attempt_id: str
    source_sha: str
    export_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    export_bytes: int = Field(ge=1, le=MAX_BYTES)
    status: Literal["PENDING"] = "PENDING"
    scenario_credit: Literal[0] = 0


class ReadPort(Protocol):
    async def run_status(
        self, *, tenant_id: UUID, run_id: UUID
    ) -> tuple[FleetRun, tuple[FleetWorker, ...]]: ...
    async def get_binding(self, slot: Slot) -> ProviderBinding | None: ...
    async def projection(self, session_id: str) -> ProjectionSnapshot | None: ...
    async def read_events(self, session_id: str, *, after: int, limit: int) -> Sequence[Event]: ...


class DatabaseReadPort:
    """Existing owner read APIs, in the caller's single read-only snapshot."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def run_status(
        self, *, tenant_id: UUID, run_id: UUID
    ) -> tuple[FleetRun, tuple[FleetWorker, ...]]:
        return await fleet.run_status(self.session, tenant_id=tenant_id, run_id=run_id)

    async def get_binding(self, slot: Slot) -> ProviderBinding | None:
        return await mux_state.get_binding(self.session, slot, read_persisted=True)

    async def projection(self, session_id: str) -> ProjectionSnapshot | None:
        return await mux_state.projection(self.session, session_id, read_persisted=True)

    async def read_events(self, session_id: str, *, after: int, limit: int) -> Sequence[Event]:
        return await mux_state.read_events(
            self.session, session_id, after=after, limit=limit, read_persisted=True
        )


def root_slot(run: FleetRun) -> Slot:
    return Slot(
        thread=ThreadRef(
            channel=ChannelRef(
                tenant_id=str(run.tenant_id),
                platform=run.platform,
                channel_id=run.channel_id,
            ),
            thread_id=run.thread_id,
        ),
        account_id=str(run.orchestrator_account_id),
    )


def safe_binding(binding: ProviderBinding) -> ProviderBinding:
    session_id = binding.native_refs.get("session")
    if not session_id:
        refuse()
    return binding.model_copy(update={"native_refs": {"session": session_id}})


async def _journal(
    reader: ReadPort,
    binding: ProviderBinding,
    *,
    worker_id: UUID | None,
    remaining: int,
) -> tuple[JournalCapture, bool, ProjectionSnapshot | None]:
    session_id = binding.native_refs["session"]
    before = await reader.projection(session_id)
    if before is not None and (
        before.session.id != session_id or before.session.provider != binding.provider
    ):
        refuse()
    records: list[JournalRecord] = []
    size = 0
    after = -1
    omitted = False
    while True:
        limit = min(1000, remaining - len(records) + 1)
        page = await reader.read_events(session_id, after=after, limit=limit)
        if len(page) > limit or len(records) + len(page) > remaining:
            refuse()
        for event in page:
            if (
                event.session_id != session_id
                or event.sequence != after + 1
                or event.native.provider != binding.provider
            ):
                refuse()
            after = event.sequence
            try:
                normalized = export_event(event, project=True)
            except RecordingError:
                normalized = None
                omitted = True
            record = JournalRecord(
                session_id=session_id,
                sequence=event.sequence,
                event_id=event.id,
                event_type=event.type,
                authority=event.authority,
                event=normalized,
                payload_omitted=normalized is None,
            )
            size += len(record.model_dump_json().encode())
            if size > MAX_BYTES:
                refuse()
            records.append(record)
        if len(page) < limit:
            break
    if await reader.projection(session_id) != before:
        refuse()
    return (
        JournalCapture(
            worker_id=worker_id,
            binding=safe_binding(binding),
            projection_cursor=before.cursor if before else None,
            records=tuple(records),
            metadata_complete=True,
        ),
        omitted,
        before,
    )


async def capture_fleet(
    reader: ReadPort,
    *,
    tenant_id: UUID,
    run_id: UUID,
    attempt_id: str,
    source_sha: str,
    max_events: int = MAX_EVENTS,
    consistency: Literal["repeatable_read", "source_rechecked"] = "source_rechecked",
) -> FleetExportV1:
    """Freeze only the requested persisted run; never claim native drain or PASS."""
    try:
        async with asyncio.timeout(120):
            return await _capture_fleet(
                reader,
                tenant_id=tenant_id,
                run_id=run_id,
                attempt_id=attempt_id,
                source_sha=source_sha,
                max_events=max_events,
                consistency=consistency,
            )
    except Exception:
        raise ExportRefused("fleet evidence capture refused") from None


async def _capture_fleet(
    reader: ReadPort,
    *,
    tenant_id: UUID,
    run_id: UUID,
    attempt_id: str,
    source_sha: str,
    max_events: int = MAX_EVENTS,
    consistency: Literal["repeatable_read", "source_rechecked"] = "source_rechecked",
) -> FleetExportV1:
    """Freeze only the requested persisted run; never claim native drain or PASS."""
    if not 1 <= max_events <= MAX_EVENTS:
        refuse()
    started = datetime.now(UTC)
    run, workers = await reader.run_status(tenant_id=tenant_id, run_id=run_id)
    if (
        (run.tenant_id, run.id) != (tenant_id, run_id)
        or any((worker.tenant_id, worker.run_id) != (tenant_id, run_id) for worker in workers)
        or len({w.id for w in workers}) != len(workers)
        or len({w.ordinal for w in workers}) != len(workers)
    ):
        refuse()
    gaps = [
        Gap(code="NATIVE_PLATFORM_CAPTURE_UNBOUND", owner="N3"),
        Gap(code="WAKE_READER_UNBOUND", owner="N5"),
        Gap(code="OUTCOME_READER_UNBOUND", owner="N4"),
        Gap(code="FINANCIAL_EXECUTION_JOIN_UNBOUND", owner="N8"),
    ]
    captures: list[FleetWorkerCapture] = []
    journals: list[JournalCapture] = []
    originals: list[tuple[Slot, ProviderBinding | None]] = []
    projections: list[tuple[str, ProjectionSnapshot | None]] = []
    remaining = max_events
    for worker in (None, *sorted(workers, key=lambda w: w.ordinal)):
        slot = root_slot(run) if worker is None else worker_slot(run, worker)
        binding = await reader.get_binding(slot)
        originals.append((slot, binding))
        result = None
        if binding is None:
            if worker is not None and (worker.binding_id is not None or worker.result is not None):
                refuse()
            gaps.append(
                Gap(code="BINDING_UNAVAILABLE", owner="N9", worker_id=worker.id if worker else None)
            )
        else:
            if binding_slot(binding) != slot or not binding.native_refs.get("session"):
                refuse()
            if worker is None:
                if (binding.id, binding.native_refs["session"]) != (
                    run.orchestrator_binding_id,
                    run.orchestrator_session_id,
                ):
                    refuse()
            elif (binding.id, str(binding.provider), binding.profile) != (
                worker.binding_id,
                worker.backend,
                worker.profile,
            ):
                refuse()
            if worker is not None and worker.result is not None:
                pointer = worker.result
                # Check the pointer session BEFORE any journal query.
                if pointer.session_id != binding.native_refs["session"]:
                    refuse()
                page = await reader.read_events(
                    pointer.session_id, after=pointer.sequence - 1, limit=1
                )
                result = page[0] if len(page) == 1 else None
                check_result(run, worker, binding, result, pointer)
                if result is None:
                    refuse()
                projected = export_event(result, project=True)
                if projected.payload != result.payload:
                    refuse()  # Never change an immutable result to fit the export.
                result = projected
            if worker is None:
                # The root session may be reused. A run row supplies no exact
                # immutable root-input boundary, so never dump its past turns.
                projection = await reader.projection(binding.native_refs["session"])
                if projection is not None and (
                    projection.session.id != run.orchestrator_session_id
                    or projection.session.provider != binding.provider
                ):
                    refuse()
                journal = JournalCapture(
                    worker_id=None,
                    binding=safe_binding(binding),
                    records=(),
                    projection_cursor=projection.cursor if projection else None,
                    metadata_complete=False,
                )
                omitted = False
                projections.append((binding.native_refs["session"], projection))
                gaps.append(Gap(code="ROOT_INPUT_JOIN_UNBOUND", owner="N4"))
            else:
                journal, omitted, projection = await _journal(
                    reader, binding, worker_id=worker.id if worker else None, remaining=remaining
                )
                projections.append((binding.native_refs["session"], projection))
            remaining -= len(journal.records)
            journals.append(journal)
            if sum(len(j.model_dump_json().encode()) for j in journals) > MAX_BYTES:
                refuse()
            if journal.projection_cursor is None:
                gaps.append(
                    Gap(
                        code="PROJECTION_UNAVAILABLE",
                        owner="N9",
                        worker_id=worker.id if worker else None,
                    )
                )
            if omitted:
                gaps.append(
                    Gap(code="PAYLOAD_OMITTED", owner="N9", worker_id=worker.id if worker else None)
                )
        if worker is not None:
            from daimon.testing.outcome_oracle import FleetWorkerFacts

            captures.append(
                FleetWorkerCapture(
                    evidence_id=f"fleet:{worker.id}:persisted",
                    attempt_id=attempt_id,
                    run_id=str(run.id),
                    root_session_id=run.orchestrator_session_id,
                    worker=FleetWorkerFacts.from_worker(worker),
                    binding=safe_binding(binding) if binding else None,
                    result=FleetResultFacts.model_validate(result) if result is not None else None,
                )
            )
    if await reader.run_status(tenant_id=tenant_id, run_id=run_id) != (run, workers):
        refuse()
    for slot, original in originals:
        if await reader.get_binding(slot) != original:
            refuse()
    for session_id, projection in projections:
        if await reader.projection(session_id) != projection:
            refuse()
    return FleetExportV1(
        tenant_id=tenant_id,
        run_id=run_id,
        attempt_id=attempt_id,
        source_sha=source_sha,
        started_at=started,
        captured_at=datetime.now(UTC),
        consistency=consistency,
        fleet=FleetEvidence(run=run, workers=tuple(captures), workers_capture_complete=True),
        journals=tuple(journals),
        worker_rows=tuple(
            WorkerPersistence(
                worker_id=w.id,
                version=w.version,
                budget_usd=w.budget_usd,
                reserved_usd=w.reserved_usd,
                deadline_at=w.deadline_at,
                max_turns=w.max_turns,
                error_code=w.error_code,
            )
            for w in workers
        ),
        gaps=tuple(gaps),
    )


def _review_text(text: str, digest: str, scanner: Audit) -> bool:
    context = frozenset((digest,))

    # Preserve duplicate keys for refusal; this is classification, not
    # the approval oracle. A rejected verdict remains exportable evidence.
    def unique(pairs: list[tuple[str, JsonValue]]) -> dict[str, JsonValue]:
        fields = dict(pairs)
        if len(fields) != len(pairs):
            raise ValueError("duplicate review field")
        return fields

    try:
        fields: JsonValue = json.loads(text, object_pairs_hook=unique)
    except ValueError:
        return False
    if not isinstance(fields, dict):
        return False
    if set(fields) != {"verdict", "reviewed_digest"} or (
        fields["reviewed_digest"] != digest or not isinstance(fields["verdict"], str)
    ):
        return False
    scanner.fragments([fields["verdict"], digest], digests={1: context})
    scanner.fragments([text], digests={0: context})
    return True


def _digest_context(value: FleetExportV1, scanner: Audit) -> dict[tuple[str, ...], frozenset[str]]:
    """Classify schema hashes and original, pointer-bound review text only."""
    contexts: dict[tuple[str, ...], frozenset[str]] = {
        ("source_sha",): frozenset((value.source_sha,))
    }

    def classify_event(event: Event | None, path: tuple[str, ...], digest: str) -> None:
        if event is None:
            return
        payload = event.typed_payload()
        if not isinstance(payload, AgentMessagePayload) or any(
            not isinstance(part, TextPart) for part in payload.content
        ):
            return
        text = "".join(part.text for part in payload.content if isinstance(part, TextPart))
        if _review_text(text, digest, scanner):
            for part in range(len(payload.content)):
                contexts[(*path, "payload", "content", str(part), "text")] = frozenset((digest,))

    by_id = {c.worker.id: c.worker for c in value.fleet.workers}
    for index, capture in enumerate(value.fleet.workers):
        worker = capture.worker
        base = ("fleet", "workers", str(index), "worker")
        for name, digest in (
            ("spec_digest", worker.spec_digest),
            ("result", worker.result.digest if worker.result else None),
        ):
            if digest and len(digest) == 64 and set(digest) <= set("0123456789abcdef"):
                path = (*base, name, "digest") if name == "result" else (*base, name)
                contexts[path] = frozenset((digest,))
        target = by_id.get(worker.reviewer_of) if worker.reviewer_of else None
        review = worker.review
        if (
            target is None
            or target.result is None
            or review is None
            or review.format != "review"
            or review.reviewed_digest != target.result.digest
        ):
            continue
        digest = target.result.digest
        if len(digest) != 64 or set(digest) - set("0123456789abcdef"):
            continue
        context = frozenset((digest,))
        contexts[(*base, "review", "reviewed_digest")] = context

        classify_event(capture.result, ("fleet", "workers", str(index), "result"), digest)
        if worker.result and _review_text(worker.result.excerpt, digest, scanner):
            contexts[(*base, "result", "excerpt")] = context
        for j, journal in enumerate(value.journals):
            if journal.worker_id == worker.id:
                for r, record in enumerate(journal.records):
                    classify_event(
                        record.event, ("journals", str(j), "records", str(r), "event"), digest
                    )
    return contexts


def _checked(export: FleetExportV1) -> bytes:
    # Revalidation also defeats model_construct/model_copy and nested mutation.
    value = FleetExportV1.model_validate_json(export.model_dump_json())
    body = (value.model_dump_json(indent=2) + "\n").encode()
    if len(body) > MAX_BYTES:
        refuse()
    scanner = Audit(())
    data = value.model_dump(mode="json")
    scanner.walk(data, 0)
    contexts = _digest_context(value, scanner)
    fields = list(string_fields(data, indexed=True))
    for stream in (fields, [(p, t) for p, t in fields if p[-1] in ("text", "delta", "content")]):
        scanner.fragments(
            [text for _, text in stream],
            digests={i: contexts[path] for i, (path, _) in enumerate(stream) if path in contexts},
        )
    return body


def _json(model: EvidenceModel) -> bytes:
    return (model.model_dump_json(indent=2) + "\n").encode()


def _digest(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def _sync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _publish_new(source: Path, target: Path) -> None:
    """Linux no-replace rename; even an existing empty directory survives a race."""
    libc = ctypes.CDLL(None, use_errno=True)
    rename = libc.renameat2
    rename.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
    rename.restype = ctypes.c_int
    if rename(-100, os.fsencode(source), -100, os.fsencode(target), 1) != 0:
        raise OSError(ctypes.get_errno(), "atomic evidence publication refused")


def publish_export(export: FleetExportV1, output: Path) -> None:
    """Validate first, then publish a fresh 0700 generation of 0600 fixed files."""
    staging: Path | None = None
    try:
        body = _checked(export)
        output = output.absolute()
        if (
            any(part in (".", "..") for part in output.parts)
            or output.name == ""
            or any(p.is_symlink() for p in (output, *output.parents))
        ):
            refuse()
        if output.exists() or not output.parent.is_dir():
            refuse()
        manifest = ExportManifest(
            tenant_id=export.tenant_id,
            run_id=export.run_id,
            attempt_id=export.attempt_id,
            source_sha=export.source_sha,
            export_sha256=_digest(body),
            export_bytes=len(body),
        )
        meta = _json(manifest)
        sums = f"{_digest(body)}  export.json\n{_digest(meta)}  manifest.json\n".encode()
        staging = Path(tempfile.mkdtemp(prefix=".fleet-export-", dir=output.parent))
        os.chmod(staging, 0o700)
        for name, data in zip(FILES, (body, meta, sums), strict=True):
            fd = os.open(
                staging / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
            )
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
        verify_export(staging)
        _sync_dir(staging)
        _publish_new(staging, output)
        staging = None
        _sync_dir(output.parent)
    except (
        ExportRefused,
        RecordingError,
        FleetConflict,
        ValidationError,
        ValueError,
        OSError,
        AttributeError,
        RecursionError,
        TypeError,
    ):
        raise ExportRefused("fleet evidence export refused") from None
    finally:
        if staging is not None:
            shutil.rmtree(staging)


def verify_export(output: Path) -> FleetExportV1:
    """Offline integrity/scope/schema check. Hashes prove no producer authority."""
    try:
        if any(p.is_symlink() for p in (output, *output.parents)) or set(
            p.name for p in output.iterdir()
        ) != set(FILES):
            refuse()
        if stat.S_IMODE(output.stat().st_mode) != 0o700:
            refuse()
        data: dict[str, bytes] = {}
        for name in FILES:
            fd = os.open(output / name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, "rb") as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600:
                    refuse()
                data[name] = stream.read(MAX_BYTES + 1)
                if len(data[name]) > MAX_BYTES:
                    refuse()
        body, meta = data["export.json"], data["manifest.json"]
        if data["SHA256SUMS"] != (
            f"{_digest(body)}  export.json\n{_digest(meta)}  manifest.json\n".encode()
        ):
            refuse()
        manifest = ExportManifest.model_validate_json(meta)
        export = FleetExportV1.model_validate_json(body)
        if (
            manifest.tenant_id,
            manifest.run_id,
            manifest.attempt_id,
            manifest.source_sha,
            manifest.export_sha256,
            manifest.export_bytes,
        ) != (
            export.tenant_id,
            export.run_id,
            export.attempt_id,
            export.source_sha,
            _digest(body),
            len(body),
        ):
            refuse()
        if _checked(export) != body:
            refuse()  # Reject duplicate-key or otherwise noncanonical payload bytes.
        return export
    except (
        ExportRefused,
        RecordingError,
        FleetConflict,
        ValidationError,
        ValueError,
        OSError,
        RecursionError,
        TypeError,
    ):
        raise ExportRefused("fleet evidence verification refused") from None


async def _capture_cli(args: argparse.Namespace) -> None:
    from daimon.core.config import DatabaseSettings
    from daimon.core.db import build_engine, build_session_factory
    from pydantic_settings import BaseSettings, SettingsConfigDict

    class ExportDatabaseSettings(BaseSettings):
        model_config = SettingsConfigDict(env_prefix="DAIMON_", env_nested_delimiter="__")
        database: DatabaseSettings

    settings = ExportDatabaseSettings.model_validate({})
    engine = build_engine(str(settings.database.url))
    try:
        async with engine.connect() as connection:
            connection = await connection.execution_options(
                isolation_level="REPEATABLE READ",
                postgresql_readonly=True,
            )
            async with connection.begin():
                await connection.execute(text("SET LOCAL statement_timeout = '10s'"))
                factory = build_session_factory(engine, crypto_keys=(), allow_plaintext=False)
                factory.configure(bind=connection, join_transaction_mode="create_savepoint")
                async with factory() as session, session.begin():
                    async with asyncio.timeout(120):
                        export = await capture_fleet(
                            DatabaseReadPort(session),
                            tenant_id=UUID(args.tenant_id),
                            run_id=UUID(args.run_id),
                            attempt_id=args.attempt_id,
                            source_sha=args.source_sha,
                            max_events=args.max_events,
                            consistency="repeatable_read",
                        )
                publish_export(export, Path(args.output))
    finally:
        await engine.dispose()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    capture = commands.add_parser(
        "capture", help="Read exact persisted run; always partial in this core"
    )
    for name in ("tenant-id", "run-id", "attempt-id", "source-sha", "output"):
        capture.add_argument(f"--{name}", required=True)
    capture.add_argument("--max-events", type=int, default=MAX_EVENTS)
    verify = commands.add_parser("verify", help="Verify offline restricted bundle")
    verify.add_argument("output", type=Path)
    args = parser.parse_args()
    try:
        if args.command == "verify":
            verify_export(args.output)
            print("PENDING: persisted bundle verified; no scenario verdict")
        else:
            asyncio.run(_capture_cli(args))
            print("PENDING: persisted fleet facts exported; owner joins incomplete")
        return 3
    except Exception:
        print("fleet evidence operation refused")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
