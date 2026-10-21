"""Service-owned SQLite/workspace snapshots stored in Azure Blob, never by tools."""

from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
from collections.abc import Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Protocol, cast
from urllib.parse import urlsplit

import zstandard
from azure.storage.blob import ContainerClient
from mux.drivers.selfhosted.transport import sandbox_id
from mux.drivers.selfhosted.wire import Event, TurnRequest
from pydantic import BaseModel, ConfigDict, Field

from .checkpoint_archive import MAX_BYTES, LimitedReader, restore_work
from .identity import CapturedIdentity
from .isolation import tool_identity, validate_credential_sources
from .journal import MAX_CURSOR, MAX_CURSOR_SLACK, Journal, identifier

MAX_ARCHIVE_BYTES = MAX_BYTES + 64 * 1024 * 1024
MAX_MANIFEST_BYTES = 128 * 1024


@dataclass
class Snapshot:
    stage: Path
    session_id: str
    seq: int
    sandbox: Path
    user: int | None
    group: int | None
    groups: list[int] | None

    def close(self) -> None:
        shutil.rmtree(self.stage)


class CheckpointStore(Protocol):
    def list(self, prefix: str) -> list[str]: ...
    def upload(self, name: str, source: BinaryIO) -> None: ...
    def download(self, name: str, target: BinaryIO) -> None: ...
    def delete(self, name: str) -> None: ...
    def close(self) -> None: ...


class AzureCheckpointStore:
    def __init__(
        self, account_url: str, container: str, identity: CapturedIdentity | None = None
    ) -> None:
        parsed = urlsplit(account_url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("checkpoint storage requires an HTTPS account origin")
        validate_credential_sources()
        if not (os.environ.get("IDENTITY_ENDPOINT") and os.environ.get("IDENTITY_HEADER")):
            raise RuntimeError("checkpoint managed identity requires a header-protected endpoint")
        self.credential = identity or CapturedIdentity()
        self.client = ContainerClient(
            account_url,
            container,
            credential=self.credential,
            connection_timeout=10,
            read_timeout=30,
            retry_total=1,
        )

    def list(self, prefix: str) -> list[str]:
        return [item.name for item in self.client.list_blobs(name_starts_with=prefix)]

    def upload(self, name: str, source: BinaryIO) -> None:
        self.client.upload_blob(name, source, overwrite=True, timeout=60)

    def download(self, name: str, target: BinaryIO) -> None:
        # Streaming bounds apply even if storage returns a malicious compressed blob.
        size = 0
        for chunk in self.client.download_blob(name, timeout=60).chunks():
            size += len(chunk)
            if size > MAX_ARCHIVE_BYTES:
                raise ValueError("checkpoint exceeds compressed size limit")
            target.write(chunk)

    def delete(self, name: str) -> None:
        self.client.delete_blob(name, timeout=60)

    def close(self) -> None:
        self.client.close()
        self.credential.close()


def configured_checkpoints(identity: CapturedIdentity | None = None) -> Checkpoints | None:
    account = os.environ.get("HARNESS_CHECKPOINT_ACCOUNT_URL")
    container = os.environ.get("HARNESS_CHECKPOINT_CONTAINER")
    if not account and not container:
        return None
    if not account or not container:
        raise ValueError("checkpoint account URL and container must both be configured")
    keep = int(os.environ.get("HARNESS_CHECKPOINT_KEEP", "3"))
    if not 1 <= keep <= 100:
        raise ValueError("checkpoint retention must be between 1 and 100")
    return Checkpoints(AzureCheckpointStore(account, container, identity), keep=keep)


def _backup(source: sqlite3.Connection, target: Path) -> None:
    with closing(sqlite3.connect(target)) as destination:
        source.backup(destination)


@contextmanager
def _scratch(home: Path) -> Iterator[Path]:
    # The parent is service-owned, so tool UIDs cannot discover or read snapshots.
    root = home / ".daimon" / "checkpoints"
    root.mkdir(mode=0o700, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="snapshot-", dir=root) as directory:
        yield Path(directory)


def sync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _remove_work(path: Path) -> None:
    os.chown(path, os.geteuid(), os.getegid(), follow_symlinks=False)
    if path.is_symlink() or not path.is_dir():
        path.unlink()
        return
    path.chmod(0o700)
    for child in path.iterdir():
        _remove_work(child)
    path.rmdir()


class WriteAhead(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request: TurnRequest
    digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    high_water: int = Field(ge=0, le=MAX_CURSOR)
    inventory: list[Event]


class Checkpoints:
    def __init__(self, store: CheckpointStore, *, keep: int = 3) -> None:
        if not 1 <= keep <= 100:
            raise ValueError("checkpoint retention must be between 1 and 100")
        self.store, self.keep = store, keep
        self.restored_seq: int | None = None

    def write_ahead(self, journal: Journal, request: TurnRequest) -> bytes:
        body = json.dumps(request.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        minimal = request.model_copy(
            update={
                "instructions": "",
                "input": [],
                "skills": [],
                "mcp_servers": [],
                "tool_policy": {},
            }
        )
        inventory = [
            event
            for event in journal.events(request.session_id)
            if event.turn_id == request.turn_id
            and (
                event.type in {"model_request_started", "usage"}
                or (event.type == "error" and event.data.get("kind") == "usage_unavailable")
            )
        ]
        return (
            WriteAhead(
                request=minimal,
                digest=hashlib.sha256(body.encode()).hexdigest(),
                high_water=journal.latest_seq(request.session_id),
                inventory=inventory,
            )
            .model_dump_json()
            .encode()
        )

    def upload_write_ahead(self, session_id: str, turn_id: str, record: bytes) -> None:
        self.store.upload(
            f"sessions/{sandbox_id(session_id)}/wal/{identifier(turn_id)}.json", io.BytesIO(record)
        )

    def restore(self, journal: Journal, session_id: str, after_seq: int = 0) -> Journal:
        pending = journal.root / "restore.pending"
        cursor_file = journal.root / "restore.cursor"
        if journal.turns() and not pending.exists() and not cursor_file.exists():
            return journal
        if cursor_file.exists():
            saved = json.loads(cursor_file.read_bytes())
            if saved["session_id"] != session_id:
                raise ValueError("restore cursor belongs to another session")
            after_seq = max(after_seq, int(saved["after_seq"]))
            self.restored_seq = int(saved["checkpoint_seq"])
        prefix = f"sessions/{sandbox_id(identifier(session_id))}/wal/"
        records: list[WriteAhead] = []
        for name in self.store.list(prefix):
            if not name.startswith(prefix) or not name.endswith(".json"):
                raise ValueError("invalid write-ahead path")
            output = io.BytesIO()
            self.store.download(name, output)
            if output.tell() > 8 * 1024 * 1024:
                raise ValueError("write-ahead record too large")
            record = WriteAhead.model_validate_json(output.getvalue())
            if record.request.session_id != session_id or name != (
                prefix + identifier(record.request.turn_id) + ".json"
            ):
                raise ValueError("foreign write-ahead record")
            if any(
                event.session_id != session_id
                or event.turn_id != record.request.turn_id
                or event.seq > record.high_water
                or event.type not in {"model_request_started", "usage", "error"}
                for event in record.inventory
            ):
                raise ValueError("invalid write-ahead inventory")
            records.append(record)
        remote_seq = max((record.high_water for record in records), default=0)
        if records and not self.names(session_id) and not pending.exists():
            cursor_file = journal.root / "restore.cursor"
            stage = cursor_file.with_suffix(".next")
            with stage.open("w") as output_file:
                json.dump(
                    {
                        "session_id": session_id,
                        "after_seq": after_seq,
                        "checkpoint_seq": 0,
                        "remote_seq": remote_seq,
                    },
                    output_file,
                )
                output_file.flush()
                os.fsync(output_file.fileno())
            stage.replace(cursor_file)
            sync_directory(journal.root)
            self.restored_seq = 0
        else:
            journal = self.restore_checkpoint(journal, session_id, after_seq, remote_seq)
        for record in sorted(records, key=lambda value: value.high_water):
            request = record.request
            existing = journal.find(request.turn_id)
            if existing is not None and journal.ended(request.turn_id):
                continue
            if existing is None:
                journal.remember_recovery(request, record.digest)
            Journal.fence_restore(
                journal.connection(session_id),
                request,
                self.restored_seq or 0,
                after_seq,
                remote_seq,
            )
            known = {
                (event.type, event.data.get("request_id"))
                for event in journal.events(session_id)
                if event.turn_id == request.turn_id
                and (
                    event.type in {"model_request_started", "usage"}
                    or (event.type == "error" and event.data.get("kind") == "usage_unavailable")
                )
            }
            for event in record.inventory:
                if (
                    event.type in {"model_request_started", "usage"}
                    or (event.type == "error" and event.data.get("kind") == "usage_unavailable")
                ) and (event.type, event.data.get("request_id")) not in known:
                    journal.append(request, event.type, event.data)
                    known.add((event.type, event.data.get("request_id")))
        return journal

    def names(self, session_id: str) -> list[tuple[int, str]]:
        prefix = f"sessions/{sandbox_id(identifier(session_id))}/"
        result: list[tuple[int, str]] = []
        for name in self.store.list(prefix):
            suffix = name.removeprefix(prefix)
            if name.startswith(prefix) and suffix.endswith(".tar.zst"):
                seq = suffix.removesuffix(".tar.zst")
                if seq.isascii() and seq.isdigit():
                    result.append((int(seq), name))
        return sorted(result)

    def capture(self, journal: Journal, session_id: str) -> Snapshot:
        """Only this short SQLite backup step runs on the journal worker."""
        seq = journal.latest_seq(session_id)
        sandbox, uid = journal.sandbox(session_id)
        user, group, groups = tool_identity(sandbox, uid)
        root = journal.root / "checkpoints"
        root.mkdir(mode=0o700, exist_ok=True)
        stage = Path(tempfile.mkdtemp(prefix="snapshot-", dir=root))
        snapshot = Snapshot(stage, session_id, seq, sandbox, user, group, groups)
        try:
            _backup(journal.db, stage / "turns.sqlite3")
            _backup(journal.connection(session_id), stage / "journal.sqlite3")
        except BaseException:
            snapshot.close()
            raise
        return snapshot

    def save(self, journal: Journal, session_id: str) -> dict[str, object]:
        return self.upload(self.capture(journal, session_id))

    def upload(self, snapshot: Snapshot) -> dict[str, object]:
        """Export, compression, storage and retention never own the journal worker."""
        stage, session_id, seq = snapshot.stage, snapshot.session_id, snapshot.seq
        sandbox, user, group, groups = (
            snapshot.sandbox,
            snapshot.user,
            snapshot.group,
            snapshot.groups,
        )
        try:
            with (
                (stage / "work.tar").open("wb") as output,
                (stage / "file.stage").open("w+b") as staged,
            ):
                exported = subprocess.run(
                    [
                        sys.executable,
                        "-I",
                        str(Path(__file__).with_name("checkpoint_archive.py")),
                        str(sandbox / "work"),
                        str(staged.fileno()),
                    ],
                    stdout=output,
                    pass_fds=(staged.fileno(),),
                    stderr=subprocess.PIPE,
                    check=True,
                    timeout=60,
                    user=user,
                    group=group,
                    extra_groups=groups,
                    env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"},
                )
            report = json.loads(exported.stderr)
            files = ("turns.sqlite3", "journal.sqlite3", "work.tar")
            if (
                sum((stage / filename).stat().st_size for filename in files)
                > MAX_ARCHIVE_BYTES - 1024 * 1024
            ):
                with tarfile.open(stage / "work.tar", "w"):
                    pass
                report.update(
                    degraded=True,
                    included_bytes=0,
                    included_files=0,
                    skipped_count=int(report["skipped_count"]) + 1,
                    skipped=[{"path": ".", "reason": "combined_archive_cap"}],
                )
            digests: dict[str, str] = {}
            total = 0
            for filename in files:
                total += (stage / filename).stat().st_size
                with (stage / filename).open("rb") as source:
                    digests[filename] = hashlib.file_digest(source, "sha256").hexdigest()
            if total > MAX_ARCHIVE_BYTES - 1024 * 1024:
                raise ValueError("checkpoint exceeds archive size limit")
            manifest = {
                "session_id": session_id,
                "seq": seq,
                "sha256": digests,
                "workspace": report,
            }
            encoded = json.dumps(manifest).encode("utf-8")
            while len(encoded) > MAX_MANIFEST_BYTES and report["skipped"]:
                report["skipped"].pop()
                encoded = json.dumps(manifest).encode("utf-8")
            if len(encoded) > MAX_MANIFEST_BYTES:
                raise ValueError("checkpoint metadata exceeds manifest budget")
            (stage / "manifest.json").write_bytes(encoded)
            archive = stage / "checkpoint.tar.zst"
            with (
                archive.open("wb") as output,
                zstandard.ZstdCompressor().stream_writer(output) as compressed,
                tarfile.open(fileobj=compressed, mode="w|") as tar,
            ):
                for name in (*files, "manifest.json"):
                    tar.add(stage / name, arcname=name, recursive=False)
            with archive.open("rb") as source:
                self.store.upload(f"sessions/{sandbox_id(session_id)}/{seq}.tar.zst", source)
            # Prune only after the new block blob is committed successfully.
            for _, name in self.names(session_id)[: -self.keep]:
                self.store.delete(name)
            return report
        finally:
            snapshot.close()

    def restore_checkpoint(
        self, journal: Journal, session_id: str, after_seq: int = 0, remote_seq: int = 0
    ) -> Journal:
        """Restore before claiming a turn, keeping the same session/seq/request ids."""
        home = journal.root.parent
        pending = journal.root / "restore.pending"
        if pending.exists() and pending.read_text() != session_id:
            raise ValueError("interrupted restore belongs to another session")
        if journal.turns() and not pending.exists():
            return journal
        checkpoints = self.names(session_id)
        if not checkpoints:
            if pending.exists() or after_seq > 0:
                raise ValueError("observed session has no remote checkpoint")
            return journal
        if not pending.exists():
            workspace_name = hashlib.sha256(session_id.encode()).hexdigest()[:32]
            work = home / "sessions" / workspace_name / "work"
            if work.exists() and any(work.iterdir()):
                raise ValueError("restore refuses to overwrite a nonempty local workspace")
            if journal.events(session_id):
                raise ValueError("restore refuses to overwrite partial local journal state")
        seq, name = checkpoints[-1]
        cursor_file = journal.root / "restore.cursor"
        if cursor_file.exists():
            saved = json.loads(cursor_file.read_bytes())
            if saved["session_id"] != session_id:
                raise ValueError("restore cursor belongs to another session")
            after_seq = max(after_seq, int(saved["after_seq"]))
        if not 0 <= after_seq <= min(MAX_CURSOR, seq + MAX_CURSOR_SLACK):
            raise ValueError("restore cursor exceeds permitted range")
        # Keep the client high-water across a crash during publication/retry.
        cursor_stage = cursor_file.with_suffix(".next")
        with cursor_stage.open("w") as output:
            json.dump(
                {
                    "session_id": session_id,
                    "after_seq": after_seq,
                    "checkpoint_seq": seq,
                    "remote_seq": remote_seq,
                },
                output,
            )
            output.flush()
            os.fsync(output.fileno())
        cursor_stage.replace(cursor_file)
        sync_directory(journal.root)
        with _scratch(home) as stage:
            archive = stage / "checkpoint.tar.zst"
            with archive.open("wb") as target:
                self.store.download(name, target)
            expected = {"manifest.json", "turns.sqlite3", "journal.sqlite3", "work.tar"}
            seen: set[str] = set()
            size = 0
            with (
                archive.open("rb") as source,
                zstandard.ZstdDecompressor().stream_reader(source) as decoded,
                tarfile.open(
                    fileobj=cast(BinaryIO, LimitedReader(decoded, MAX_ARCHIVE_BYTES)), mode="r|"
                ) as tar,
            ):
                for member in tar:
                    size += member.size
                    if (
                        member.name not in expected
                        or member.name in seen
                        or not member.isfile()
                        or member.size < 0
                        or size > MAX_ARCHIVE_BYTES
                        or (member.name == "manifest.json" and member.size > MAX_MANIFEST_BYTES)
                    ):
                        raise ValueError("invalid checkpoint archive")
                    seen.add(member.name)
                    content = tar.extractfile(member)
                    if content is None:
                        raise ValueError("missing checkpoint file")
                    with (stage / member.name).open("xb") as output:
                        shutil.copyfileobj(content, output, length=1024 * 1024)
            if seen != expected:
                raise ValueError("incomplete checkpoint archive")
            manifest = json.loads((stage / "manifest.json").read_bytes())
            if manifest.get("session_id") != session_id or manifest.get("seq") != seq:
                raise ValueError("checkpoint session or sequence mismatch")
            for filename in expected - {"manifest.json"}:
                with (stage / filename).open("rb") as source:
                    digest = hashlib.file_digest(source, "sha256").hexdigest()
                if manifest.get("sha256", {}).get(filename) != digest:
                    raise ValueError("checkpoint digest mismatch")
            with closing(sqlite3.connect(stage / "turns.sqlite3")) as db:
                sessions = {
                    row[0]
                    for row in db.execute(
                        "SELECT session_id FROM turns UNION SELECT session_id FROM session_binding "
                        "UNION SELECT session_id FROM sandboxes"
                    )
                }
                if sessions != {session_id} or db.execute("PRAGMA integrity_check").fetchone() != (
                    "ok",
                ):
                    raise ValueError("checkpoint contains invalid session state")
                latest = db.execute(
                    "SELECT request FROM turns ORDER BY rowid DESC LIMIT 1"
                ).fetchone()
                request = TurnRequest.model_validate_json(latest[0])
            with closing(sqlite3.connect(stage / "journal.sqlite3")) as db:
                if (
                    db.execute("PRAGMA integrity_check").fetchone() != ("ok",)
                    or db.execute("SELECT COALESCE(MAX(seq), 0) FROM events").fetchone()[0] != seq
                ):
                    raise ValueError("checkpoint journal is inconsistent")
                Journal.fence_restore(db, request, seq, after_seq, remote_seq)
            with (stage / "work.tar").open("rb") as source:
                restore_work(source, stage / "work")
            # A marker makes publication restartable if the process dies between renames.
            with pending.open("w") as marker:
                marker.write(session_id)
                marker.flush()
                os.fsync(marker.fileno())
            sync_directory(journal.root)
            journal.close()
            for path in (
                journal.root / "turns.sqlite3",
                journal.root / "sessions" / session_id / "journal.sqlite3",
            ):
                path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                for suffix in ("-wal", "-shm"):
                    path.with_name(path.name + suffix).unlink(missing_ok=True)
                (stage / path.name).replace(path)
                path.chmod(0o600)
                with path.open("rb") as source:
                    os.fsync(source.fileno())
                sync_directory(path.parent)
            restored = Journal(home)
            sandbox, uid = restored.sandbox(session_id)
            tool_identity(sandbox, uid)
            work = sandbox / "work"
            # No tool is running during restore. Reclaim any partial publication
            # before replacing it, without following workspace links.
            _remove_work(work)
            incoming = stage / "work"
            user = tool_identity(sandbox, uid)[0]
            paths = sorted(incoming.rglob("*"), key=lambda p: len(p.parts), reverse=True)
            for path in [*paths, incoming]:
                if path.is_file():
                    with path.open("rb") as source:
                        os.fsync(source.fileno())
                else:
                    sync_directory(path)
            incoming.replace(work)
            if user is not None:
                for path in [
                    *sorted(work.rglob("*"), key=lambda p: len(p.parts), reverse=True),
                    work,
                ]:
                    os.chown(path, user, user)
            sync_directory(sandbox)
            sync_directory(sandbox.parent)
            sync_directory(journal.root / "sessions")
            sync_directory(home)
            pending.unlink()
            sync_directory(journal.root)
            self.restored_seq = seq
            return restored

    def close(self) -> None:
        self.store.close()
