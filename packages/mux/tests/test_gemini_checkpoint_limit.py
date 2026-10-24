"""Large workspace members survive the offline Gemini snapshot artifact path."""

import gzip
import hashlib
import io
import tarfile
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
from mux.contracts.actions import UserMessage
from mux.contracts.events import TextPart
from mux.contracts.extensions import ExtensionConfig
from mux.contracts.ids import ModelRef, PageRequest, Scope
from mux.contracts.resources import AgentSpec, EnvironmentSpec, Session, SessionSpec
from mux.drivers.gemini import GeminiManagedAgents, resources
from mux.errors import ProviderError, ScopeViolation
from mux.state.memory import MemoryStateStore
from mux.testing.drivers.gemini.fake import FakeTransport, MemoryStorage

pytestmark = pytest.mark.usefixtures("gemini_recorded_clock")

MIB = 1024 * 1024
SCOPE = Scope(tenant_id="t", account_id="a", principal_id="p", authorization_id="allowed")


def archive(files: dict[str, bytes], *, compressed: bool = True) -> bytes:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz" if compressed else "w:") as tar:
        for path, body in files.items():
            info = tarfile.TarInfo(path)
            info.size = len(body)
            tar.addfile(info, io.BytesIO(body))
    return output.getvalue()


def binary_member() -> bytes:
    return bytes(range(256)) * (40 * MIB // 256)


def backend() -> tuple[GeminiManagedAgents, FakeTransport, MemoryStorage]:
    transport, storage = FakeTransport(), MemoryStorage()
    return (
        GeminiManagedAgents(
            transport,
            storage=storage,
            state_store=MemoryStateStore(),
            account_scope_id="project",
        ),
        transport,
        storage,
    )


async def completed_workspace(ma: GeminiManagedAgents, transport: FakeTransport) -> Session:
    agent = await ma.agents.create(
        SCOPE,
        AgentSpec(name="a", model=ModelRef(provider="gemini", id="gemini-3.8-flash")),
        key="agent",
    )
    environment = await ma.environments.create(SCOPE, EnvironmentSpec(name="e"), key="env")
    session = await ma.sessions.create(
        SCOPE,
        SessionSpec(
            agent=agent.ref,
            agent_revision=agent.revision,
            environment=environment.ref,
            config_revision=1,
            extensions={
                "gemini.session": ExtensionConfig(
                    namespace="gemini.session",
                    version=1,
                    value={
                        "thread": {
                            "channel": {"tenant_id": "t", "platform": "discord", "channel_id": "c"},
                            "thread_id": "th",
                        },
                        "binding_id": "binding",
                    },
                )
            },
        ),
        key="session",
    )
    stamp = datetime(2026, 10, 10, tzinfo=UTC).isoformat()
    transport.responses.append(
        {
            "id": "turn-1",
            "status": "completed",
            "created": stamp,
            "updated": stamp,
            "environment_id": "workspace-1",
            "steps": [],
            "usage": None,
        }
    )
    await ma.events.send(
        SCOPE, session.ref, (UserMessage(content=(TextPart(text="checkpoint"),)),), key="turn"
    )
    return session


@pytest.mark.parametrize("compressed", [False, True])
def test_snapshot_accepts_a_real_40_mib_binary_member(
    compressed: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = binary_member()
    snapshot = archive({"work/large.bin": body}, compressed=compressed)
    reads: list[int] = []
    original_read = tarfile.ExFileObject.read

    def bounded_read(source: tarfile.ExFileObject, size: int = -1) -> bytes:
        assert 0 < size <= 65536
        reads.append(size)
        return original_read(source, size)

    monkeypatch.setattr(tarfile.ExFileObject, "read", bounded_read)
    parsed = resources.parse_snapshot(snapshot)
    assert set(parsed) == {"work/large.bin"}
    assert len(parsed["work/large.bin"]) == 40 * MIB
    assert hashlib.sha256(parsed["work/large.bin"]).digest() == hashlib.sha256(body).digest()
    assert sum(reads) == 40 * MIB and len(reads) == 640


async def test_public_snapshot_listing_and_chunked_download_preserve_40_mib() -> None:
    ma, transport, _ = backend()
    session = await completed_workspace(ma, transport)
    body = binary_member()
    expected = hashlib.sha256(body).hexdigest()
    transport.snapshots["workspace-1"] = archive({"work/large.bin": body})
    listing = await ma.artifacts.list(SCOPE, session.ref, page=PageRequest(limit=1))
    assert not listing.has_more and len(listing.data) == 1
    artifact = listing.data[0]
    assert artifact.filename == "work/large.bin" and artifact.size_bytes == 40 * MIB
    assert artifact.session == session.ref and artifact.turn_id == "turn-1"
    assert artifact.native is not None and isinstance(artifact.native, dict)
    assert artifact.native["sha256"] == expected
    digest = hashlib.sha256()
    count, size = 0, 0
    async for chunk in ma.artifacts.download(SCOPE, artifact.ref):
        assert 0 < len(chunk) <= 65536
        digest.update(chunk)
        size += len(chunk)
        count += 1
    assert size == 40 * MIB and count == 640 and digest.hexdigest() == expected
    assert transport.snapshot_reads == ["workspace-1", "workspace-1"]
    assert len(transport.requests) == 1 and not transport.responses
    before = list(transport.snapshot_reads)
    foreign = SCOPE.model_copy(update={"account_id": "foreign"})
    with pytest.raises(ScopeViolation):
        _ = [part async for part in ma.artifacts.download(foreign, artifact.ref)]
    assert transport.snapshot_reads == before


@pytest.mark.parametrize("size", [1024, 1025])
def test_snapshot_member_budget_boundary(size: int, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(resources, "MAX_SNAPSHOT_MEMBER_BYTES", 1024)
    snapshot = archive({"file.bin": b"x" * size})
    if size == 1024:
        assert resources.parse_snapshot(snapshot) == {"file.bin": b"x" * size}
    else:
        with pytest.raises(ProviderError, match="invalid_snapshot"):
            resources.parse_snapshot(snapshot)


def test_snapshot_rejects_truncated_member() -> None:
    snapshot = archive({"file.bin": b"x" * 1024}, compressed=False)
    with pytest.raises(ProviderError, match="invalid_snapshot"):
        resources.parse_snapshot(snapshot[: 512 + 32])


def test_snapshot_rejects_negative_declared_member_size() -> None:
    info = tarfile.TarInfo("negative.bin")
    info.size = -1
    snapshot = info.tobuf(format=tarfile.GNU_FORMAT) + b"\0" * 1024
    with pytest.raises(ProviderError, match="invalid_snapshot"):
        resources.parse_snapshot(snapshot)


def test_snapshot_gzip_expansion_is_still_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    snapshot = archive({"file.bin": b"x" * 4096}, compressed=False)
    monkeypatch.setattr(resources, "MAX_SNAPSHOT_BYTES", 2048)
    compressed = gzip.compress(snapshot)
    assert len(compressed) < 2048 < len(snapshot)
    with pytest.raises(ProviderError, match="snapshot_too_large"):
        resources.parse_snapshot(compressed)


async def test_ordinary_upload_still_refuses_above_16_mib_and_closes_source() -> None:
    ma, transport, storage = backend()
    closed: list[bool] = []

    async def source() -> AsyncIterator[bytes]:
        try:
            for _ in range(16 * MIB // 65536):
                yield b"x" * 65536
            yield b"x"
        finally:
            closed.append(True)

    with pytest.raises(ProviderError, match="artifact_too_large"):
        await ma.artifacts.upload(
            SCOPE,
            source(),
            filename="upload.bin",
            media_type="application/octet-stream",
            key="large",
        )
    assert closed == [True] and not transport.requests and not transport.snapshot_reads
    async with storage.transaction() as records:
        assert not records.artifacts and not records.creations
