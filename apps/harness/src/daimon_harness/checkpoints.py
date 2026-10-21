"""Service-owned SQLite/workspace snapshots stored in Azure Blob, never by tools."""

from __future__ import annotations

import hashlib
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
from pathlib import Path
from typing import BinaryIO, Protocol, cast
from urllib.parse import urlsplit

import zstandard
from azure.storage.blob import ContainerClient
from mux.drivers.selfhosted.transport import sandbox_id

from .checkpoint_archive import MAX_BYTES, LimitedReader, restore_work
from .identity import CapturedIdentity
from .isolation import tool_identity, validate_credential_sources
from .journal import Journal, identifier

MAX_ARCHIVE_BYTES = MAX_BYTES + 64 * 1024 * 1024


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
        self.client = ContainerClient(account_url, container, credential=self.credential)

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


class Checkpoints:
    def __init__(self, store: CheckpointStore, *, keep: int = 3) -> None:
        if not 1 <= keep <= 100:
            raise ValueError("checkpoint retention must be between 1 and 100")
        self.store, self.keep = store, keep
        self.restored_seq: int | None = None

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

    def save(self, journal: Journal, session_id: str) -> dict[str, object]:
        events = journal.events(session_id)
        seq = events[-1].seq if events else 0
        sandbox, uid = journal.sandbox(session_id)
        user, group, groups = tool_identity(sandbox, uid)
        with _scratch(journal.root.parent) as stage:
            _backup(journal.db, stage / "turns.sqlite3")
            _backup(journal.connection(session_id), stage / "journal.sqlite3")
            with (stage / "work.tar").open("wb") as output:
                exported = subprocess.run(
                    [
                        sys.executable,
                        "-I",
                        str(Path(__file__).with_name("checkpoint_archive.py")),
                        str(sandbox / "work"),
                    ],
                    stdout=output,
                    stderr=subprocess.PIPE,
                    check=True,
                    timeout=60,
                    user=user,
                    group=group,
                    extra_groups=groups,
                    env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"},
                )
            files = ("turns.sqlite3", "journal.sqlite3", "work.tar")
            digests: dict[str, str] = {}
            total = 0
            for filename in files:
                total += (stage / filename).stat().st_size
                with (stage / filename).open("rb") as source:
                    digests[filename] = hashlib.file_digest(source, "sha256").hexdigest()
            if total > MAX_ARCHIVE_BYTES - 1024 * 1024:
                raise ValueError("checkpoint exceeds archive size limit")
            report = json.loads(exported.stderr)
            manifest = {
                "session_id": session_id,
                "seq": seq,
                "sha256": digests,
                "workspace": report,
            }
            (stage / "manifest.json").write_text(json.dumps(manifest))
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

    def restore(self, journal: Journal, session_id: str) -> Journal:
        """Restore before claiming a turn, keeping the same session/seq/request ids."""
        home = journal.root.parent
        pending = journal.root / "restore.pending"
        if pending.exists() and pending.read_text() != session_id:
            raise ValueError("interrupted restore belongs to another session")
        if journal.turns() and not pending.exists():
            return journal
        checkpoints = self.names(session_id)
        if not checkpoints:
            if pending.exists():
                raise ValueError("interrupted restore has no remote checkpoint")
            return journal
        if not pending.exists():
            workspace_name = hashlib.sha256(session_id.encode()).hexdigest()[:32]
            work = home / "sessions" / workspace_name / "work"
            if work.exists() and any(work.iterdir()):
                raise ValueError("restore refuses to overwrite a nonempty local workspace")
            if journal.events(session_id):
                raise ValueError("restore refuses to overwrite partial local journal state")
        seq, name = checkpoints[-1]
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
                        or (member.name == "manifest.json" and member.size > 128 * 1024)
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
            with closing(sqlite3.connect(stage / "journal.sqlite3")) as db:
                if (
                    db.execute("PRAGMA integrity_check").fetchone() != ("ok",)
                    or db.execute("SELECT COALESCE(MAX(seq), 0) FROM events").fetchone()[0] != seq
                ):
                    raise ValueError("checkpoint journal is inconsistent")
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
