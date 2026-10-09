"""Offline binary snapshot and immutable inline bundle proofs."""

import io
import tarfile
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
from mux.contracts.actions import UserMessage
from mux.contracts.events import TextPart
from mux.contracts.extensions import ExtensionConfig
from mux.contracts.ids import ModelRef, PageRequest, Scope
from mux.contracts.resources import (
    AgentSpec,
    EnvironmentSpec,
    Session,
    SessionSpec,
    SkillUpload,
    SkillUploadFile,
    WorkspaceSource,
)
from mux.drivers.gemini import GeminiManagedAgents
from mux.drivers.gemini.fake import FakeTransport, MemoryStorage
from mux.drivers.gemini.resources import parse_snapshot
from mux.errors import (
    ContinuityLost,
    OperationConflict,
    ProviderError,
    ScopeViolation,
    UnsupportedCapability,
)
from mux.state.memory import MemoryStateStore

SCOPE = Scope(tenant_id="t", account_id="a", principal_id="p", authorization_id="allowed")
type Resources = tuple[GeminiManagedAgents, FakeTransport, MemoryStorage, MemoryStateStore]


@pytest.fixture
async def resources() -> Resources:
    transport, storage, state = FakeTransport(), MemoryStorage(), MemoryStateStore()
    return (
        GeminiManagedAgents(
            transport, storage=storage, state_store=state, account_scope_id="project"
        ),
        transport,
        storage,
        state,
    )


def bundle(body: bytes = b"# Original", title: str = "original") -> SkillUpload:
    return SkillUpload(
        display_title=title,
        files=(
            SkillUploadFile(path="SKILL.md", content=body),
            SkillUploadFile(path="scripts/run.py", content=b"print(1)\n"),
        ),
    )


async def session(ma: GeminiManagedAgents, agent: AgentSpec, env: EnvironmentSpec) -> Session:
    a = await ma.agents.create(SCOPE, agent, key="agent")
    e = await ma.environments.create(SCOPE, env, key="env")
    return await ma.sessions.create(
        SCOPE,
        SessionSpec(
            agent=a.ref,
            agent_revision=a.revision,
            environment=e.ref,
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


async def send(
    ma: GeminiManagedAgents,
    transport: FakeTransport,
    s: Session,
    key: str = "send",
    id_: str = "i1",
) -> None:
    stamp = datetime.now(UTC).isoformat()
    transport.responses.append(
        {
            "id": id_,
            "status": "completed",
            "created": stamp,
            "updated": stamp,
            "environment_id": "e1",
            "steps": [],
            "usage": None,
        }
    )
    await ma.events.send(SCOPE, s.ref, (UserMessage(content=(TextPart(text="hello"),)),), key=key)


async def chunks(body: bytes) -> AsyncIterator[bytes]:
    yield body[:3]
    yield body[3:]


def snapshot(files: dict[str, bytes]) -> bytes:
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w:gz") as archive:
        for path, data in files.items():
            info = tarfile.TarInfo(path)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return out.getvalue()


@pytest.mark.asyncio
async def test_pinned_bundle_survives_publish_restart_and_mounts_exact_sources(
    resources: Resources,
) -> None:
    ma, transport, storage, state = resources
    original = await ma.skills.create(SCOPE, bundle(), key="skill")
    assert original.latest_version is not None
    s = await session(
        ma,
        AgentSpec(
            name="a",
            model=ModelRef(provider="gemini", id="gemini-3.8-flash"),
            skills=(original.latest_version,),
        ),
        EnvironmentSpec(name="e"),
    )
    newer = await ma.skills.publish_version(
        SCOPE, original.id, bundle(b"# New", "new"), key="publish"
    )
    assert newer.ref != original.latest_version
    ma = GeminiManagedAgents(
        transport, storage=storage, state_store=state.restart(), account_scope_id="project"
    )
    assert await ma.skills.create(SCOPE, bundle(), key="skill") == original
    assert (
        await ma.skills.publish_version(SCOPE, original.id, bundle(b"# New", "new"), key="publish")
        == newer
    )
    await send(ma, transport, s)
    assert transport.requests[0]["environment"] == {
        "type": "remote",
        "sources": [
            {
                "type": "inline",
                "target": f".agents/skills/{original.id}/SKILL.md",
                "content": "# Original",
            },
            {
                "type": "inline",
                "target": f".agents/skills/{original.id}/scripts/run.py",
                "content": "print(1)\n",
            },
        ],
    }
    await send(ma, transport, s, "second", "i2")
    assert transport.requests[1]["environment"] == "e1"
    with pytest.raises(ProviderError, match="skill_in_use"):
        await ma.skills.delete(SCOPE, original.id, key="delete")


@pytest.mark.asyncio
async def test_host_upload_exact_binary_replay_and_scope_without_provider_io(
    resources: Resources,
) -> None:
    ma, transport, storage, state = resources
    data = b"\x00\xff" + bytes(range(256)) * 300
    first = await ma.artifacts.upload(
        SCOPE, chunks(data), filename="raw.bin", media_type="application/octet-stream", key="upload"
    )
    ma = GeminiManagedAgents(
        transport, storage=storage, state_store=state.restart(), account_scope_id="project"
    )
    assert (
        await ma.artifacts.upload(
            SCOPE,
            chunks(data),
            filename="raw.bin",
            media_type="application/octet-stream",
            key="upload",
        )
        == first
    )
    assert b"".join([part async for part in ma.artifacts.download(SCOPE, first.ref)]) == data
    with pytest.raises(OperationConflict):
        await ma.artifacts.upload(
            SCOPE,
            chunks(b"changed"),
            filename="raw.bin",
            media_type="application/octet-stream",
            key="upload",
        )
    for change in ({"tenant_id": "other"}, {"account_id": "other"}, {"account_scope_id": "other"}):
        with pytest.raises(ScopeViolation):
            await ma.artifacts.retrieve(SCOPE, first.ref.model_copy(update=change))
    with pytest.raises(ScopeViolation):
        await ma.artifacts.upload(
            SCOPE.model_copy(update={"principal_id": "other"}),
            chunks(data),
            filename="raw.bin",
            media_type="application/octet-stream",
            key="upload",
        )
    receipt = await ma.artifacts.delete(SCOPE, first.ref, key="delete")
    assert await ma.artifacts.delete(SCOPE, first.ref, key="delete") == receipt
    assert not transport.requests and not transport.snapshot_reads


@pytest.mark.asyncio
async def test_inline_uploaded_text_mount_and_binary_mount_refusal(resources: Resources) -> None:
    ma, transport, _, _ = resources
    text = await ma.artifacts.upload(
        SCOPE, chunks(b"hello\n"), filename="input.txt", media_type="text/plain", key="upload"
    )
    env = EnvironmentSpec(
        name="e",
        sources=(WorkspaceSource(kind="file", target_path="inputs/input.txt", artifact=text.ref),),
    )
    s = await session(
        ma, AgentSpec(name="a", model=ModelRef(provider="gemini", id="gemini-3.8-flash")), env
    )
    await send(ma, transport, s)
    assert transport.requests[0]["environment"] == {
        "type": "remote",
        "sources": [{"type": "inline", "target": "inputs/input.txt", "content": "hello\n"}],
    }
    with pytest.raises(ProviderError, match="artifact_in_use"):
        await ma.artifacts.delete(SCOPE, text.ref, key="delete")
    binary = await ma.artifacts.upload(
        SCOPE,
        chunks(b"\xff"),
        filename="binary",
        media_type="application/octet-stream",
        key="binary",
    )
    with pytest.raises(UnsupportedCapability, match="binary_inline_source"):
        await ma.environments.create(
            SCOPE,
            env.model_copy(
                update={
                    "sources": (
                        WorkspaceSource(kind="file", target_path="binary", artifact=binary.ref),
                    )
                }
            ),
            key="binary-env",
        )
    assert len(transport.requests) == 1


@pytest.mark.asyncio
async def test_output_snapshot_pagination_is_stable_and_download_revalidates_bytes(
    resources: Resources,
) -> None:
    ma, transport, storage, state = resources
    s = await session(
        ma,
        AgentSpec(name="a", model=ModelRef(provider="gemini", id="gemini-3.8-flash")),
        EnvironmentSpec(name="e"),
    )
    await send(ma, transport, s)
    data = b"\x00\xff" + bytes(range(256)) * 300
    transport.snapshots["e1"] = snapshot({"a.bin": data, "nested/b.txt": b"B"})
    first = await ma.artifacts.list(SCOPE, s.ref, page=PageRequest(limit=1))
    assert first.has_more and first.next_cursor is not None
    filtered = await ma.artifacts.list(
        SCOPE, s.ref, page=PageRequest(limit=1), turn_id=first.data[0].turn_id
    )
    assert filtered.next_cursor != first.next_cursor
    ma = GeminiManagedAgents(
        transport, storage=storage, state_store=state.restart(), account_scope_id="project"
    )
    transport.snapshots["e1"] = snapshot({"a.bin": data, "c.txt": b"new"})
    second = await ma.artifacts.list(
        SCOPE, s.ref, page=PageRequest(limit=1, cursor=first.next_cursor)
    )
    assert second.data[0].filename == "nested/b.txt" and not second.has_more
    assert transport.snapshot_reads == ["e1"]
    assert (
        b"".join([part async for part in ma.artifacts.download(SCOPE, first.data[0].ref)]) == data
    )
    with pytest.raises(ProviderError, match="artifact_unavailable"):
        _ = [part async for part in ma.artifacts.download(SCOPE, second.data[0].ref)]
    transport.snapshots["e1"] = snapshot({"a.bin": b"changed"})
    with pytest.raises(ProviderError, match="artifact_changed"):
        _ = [part async for part in ma.artifacts.download(SCOPE, first.data[0].ref)]
    with pytest.raises(UnsupportedCapability, match="workspace_artifact_delete"):
        await ma.artifacts.delete(SCOPE, first.data[0].ref, key="delete")
    transport.snapshots["e1"] = ProviderError("not_found", retryable=False)
    with pytest.raises(ContinuityLost) as lost:
        _ = [part async for part in ma.artifacts.download(SCOPE, first.data[0].ref)]
    assert lost.value.binding_id == "binding"


@pytest.mark.parametrize("path", ["../escape", "/absolute", "a/../b", "a\\b"])
def test_snapshot_rejects_unsafe_paths(path: str) -> None:
    with pytest.raises(ProviderError, match="invalid_snapshot"):
        parse_snapshot(snapshot({path: b"data"}))


@pytest.mark.parametrize("kind", [tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.FIFOTYPE])
def test_snapshot_rejects_links_and_special_files(kind: bytes) -> None:
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w") as archive:
        info = tarfile.TarInfo("unsafe")
        info.type, info.linkname = kind, "../../outside"
        archive.addfile(info)
    with pytest.raises(ProviderError, match="invalid_snapshot"):
        parse_snapshot(out.getvalue())


def test_snapshot_rejects_duplicate_normalized_paths_and_malformed_archives() -> None:
    with pytest.raises(ProviderError, match="invalid_snapshot"):
        parse_snapshot(snapshot({"same": b"1", "./same": b"2"}))
    with pytest.raises(ProviderError, match="invalid_snapshot"):
        parse_snapshot(b"not a tar archive")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "files",
    [
        (SkillUploadFile(path="other", content=b"x"),),
        (SkillUploadFile(path="../SKILL.md", content=b"x"),),
        (
            SkillUploadFile(path="SKILL.md", content=b"x"),
            SkillUploadFile(path="SKILL.md", content=b"y"),
        ),
    ],
)
async def test_invalid_bundles_refuse_before_provider_io(
    resources: Resources, files: tuple[SkillUploadFile, ...]
) -> None:
    ma, transport, _, _ = resources
    with pytest.raises(ValueError):
        await ma.skills.create(SCOPE, SkillUpload(files=files), key="invalid")
    assert not transport.requests


@pytest.mark.asyncio
async def test_skill_scope_delete_and_cross_operation_key_replay(resources: Resources) -> None:
    ma, _, _, _ = resources
    skill = await ma.skills.create(SCOPE, bundle(), key="skill")
    with pytest.raises(ScopeViolation):
        await ma.skills.retrieve(SCOPE.model_copy(update={"tenant_id": "other"}), skill.id)
    receipt = await ma.skills.delete(SCOPE, skill.id, key="delete")
    assert await ma.skills.delete(SCOPE, skill.id, key="delete") == receipt
    with pytest.raises(OperationConflict):
        await ma.skills.create(SCOPE, bundle(), key="delete")


@pytest.mark.asyncio
async def test_oversized_upload_closes_input_and_commits_nothing(
    resources: Resources, monkeypatch: pytest.MonkeyPatch
) -> None:
    ma, transport, _, _ = resources
    monkeypatch.setattr("mux.drivers.gemini.resources.MAX_ARTIFACT_BYTES", 4)
    closed: list[bool] = []

    async def source() -> AsyncIterator[bytes]:
        try:
            yield b"12345"
        finally:
            closed.append(True)

    with pytest.raises(ProviderError, match="artifact_too_large"):
        await ma.artifacts.upload(
            SCOPE, source(), filename="large", media_type="text/plain", key="large"
        )
    assert closed == [True]
    valid = await ma.artifacts.upload(
        SCOPE, chunks(b"1234"), filename="large", media_type="text/plain", key="large"
    )
    assert valid.size_bytes == 4 and not transport.requests


def test_snapshot_enforces_compressed_and_expanded_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    archive = snapshot({"large": b"x" * 1000})
    monkeypatch.setattr("mux.drivers.gemini.resources.MAX_SNAPSHOT_BYTES", len(archive) - 1)
    with pytest.raises(ProviderError, match="snapshot_too_large"):
        parse_snapshot(archive)
    monkeypatch.setattr("mux.drivers.gemini.resources.MAX_SNAPSHOT_BYTES", 20000)
    monkeypatch.setattr("mux.drivers.gemini.resources.MAX_ARTIFACT_BYTES", 999)
    with pytest.raises(ProviderError, match="invalid_snapshot"):
        parse_snapshot(archive)
    monkeypatch.setattr("mux.drivers.gemini.resources.MAX_ARTIFACT_BYTES", 1000)
    monkeypatch.setattr("mux.drivers.gemini.resources.MAX_SNAPSHOT_BYTES", 1999)
    with pytest.raises(ProviderError, match="snapshot_too_large"):
        parse_snapshot(snapshot({"a": b"a" * 1000, "b": b"b" * 1000}))


@pytest.mark.asyncio
async def test_mount_alias_overlap_with_pinned_skills_refuses_before_io(
    resources: Resources,
) -> None:
    ma, transport, _, _ = resources
    skill = await ma.skills.create(SCOPE, bundle(), key="skill")
    assert skill.latest_version is not None
    artifact = await ma.artifacts.upload(
        SCOPE, chunks(b"override"), filename="SKILL.md", media_type="text/plain", key="input"
    )
    with pytest.raises(ValueError, match="overlap"):
        await session(
            ma,
            AgentSpec(
                name="a",
                model=ModelRef(provider="gemini", id="gemini-3.8-flash"),
                skills=(skill.latest_version,),
            ),
            EnvironmentSpec(
                name="e",
                sources=(
                    WorkspaceSource(
                        kind="file",
                        target_path=f"/workspace/.agents/skills/{skill.id}/SKILL.md",
                        artifact=artifact.ref,
                    ),
                ),
            ),
        )
    assert not transport.requests


@pytest.mark.asyncio
async def test_foreign_snapshot_query_and_cursor_fail_before_io(resources: Resources) -> None:
    ma, transport, _, _ = resources
    s = await session(
        ma,
        AgentSpec(name="a", model=ModelRef(provider="gemini", id="gemini-3.8-flash")),
        EnvironmentSpec(name="e"),
    )
    await send(ma, transport, s)
    transport.snapshots["e1"] = snapshot({"a": b"A", "b": b"B"})
    first = await ma.artifacts.list(SCOPE, s.ref, page=PageRequest(limit=1))
    assert first.next_cursor is not None
    for ref in (
        s.ref.model_copy(update={"tenant_id": "foreign"}),
        s.ref.model_copy(update={"account_scope_id": "foreign"}),
    ):
        with pytest.raises(ScopeViolation):
            await ma.artifacts.list(SCOPE, ref, page=PageRequest())
    with pytest.raises(ScopeViolation):
        await ma.artifacts.list(
            SCOPE, s.ref, page=PageRequest(cursor=first.next_cursor, order="desc")
        )
    assert transport.snapshot_reads == ["e1"]


@pytest.mark.asyncio
async def test_snapshot_turn_race_refuses_catalogue_commit(
    resources: Resources, monkeypatch: pytest.MonkeyPatch
) -> None:
    ma, transport, storage, _ = resources
    s = await session(
        ma,
        AgentSpec(name="a", model=ModelRef(provider="gemini", id="gemini-3.8-flash")),
        EnvironmentSpec(name="e"),
    )
    await send(ma, transport, s)

    async def raced_download(environment_id: str) -> bytes:
        assert environment_id == "e1"
        await send(ma, transport, s, "new-turn", "i2")
        return snapshot({"stale": b"old"})

    monkeypatch.setattr(transport, "download_snapshot", raced_download)
    with pytest.raises(ProviderError, match="snapshot_turn_changed"):
        await ma.artifacts.list(SCOPE, s.ref, page=PageRequest())
    async with storage.transaction() as records:
        assert not records.artifacts and not records.artifact_pages


@pytest.mark.asyncio
async def test_snapshot_network_failure_returns_typed_error_without_partial_bytes(
    resources: Resources,
) -> None:
    ma, transport, _, _ = resources
    s = await session(
        ma,
        AgentSpec(name="a", model=ModelRef(provider="gemini", id="gemini-3.8-flash")),
        EnvironmentSpec(name="e"),
    )
    await send(ma, transport, s)
    transport.snapshots["e1"] = snapshot({"binary": bytes(range(256))})
    found = await ma.artifacts.list(SCOPE, s.ref, page=PageRequest())
    transport.snapshots["e1"] = ProviderError("transient_network", retryable=True)
    emitted: list[bytes] = []
    with pytest.raises(ProviderError) as exc:
        async for part in ma.artifacts.download(SCOPE, found.data[0].ref):
            emitted.append(part)
    assert exc.value.category == "transient_network" and not emitted


def test_snapshot_refuses_directory_payload_before_skipping_it() -> None:
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w") as archive:
        info = tarfile.TarInfo("directory")
        info.type, info.size = tarfile.DIRTYPE, 10
        archive.addfile(info, io.BytesIO(b"1234567890"))
    with pytest.raises(ProviderError, match="invalid_snapshot"):
        parse_snapshot(out.getvalue())
