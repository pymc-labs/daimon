"""Skills past the inline limits: uploaded into an environment made before the first turn."""

import asyncio
import io
import json
import tarfile
from collections.abc import Callable
from datetime import UTC, datetime
from hashlib import sha256

import httpx
import pytest
from mux.contracts.actions import UserMessage
from mux.contracts.events import TextPart
from mux.contracts.extensions import ExtensionConfig
from mux.contracts.ids import ModelRef, Scope, SkillRef
from mux.contracts.resources import AgentSpec, Session, SessionSpec, SkillUpload, SkillUploadFile
from mux.drivers.gemini import GeminiManagedAgents, Records
from mux.drivers.gemini.bundles import (
    mounted_files,
    skill_archive,
    upload_manifest,
    validate_bundle,
)
from mux.drivers.gemini.catalog import native_definition
from mux.drivers.gemini.transport import Object, SDKTransport
from mux.errors import ContinuityLost, ProviderError, UnsupportedCapability
from mux.state.memory import MemoryStateStore
from mux.testing.drivers.gemini.fake import FakeTransport, MemoryStorage
from mux.testing.isolated_sdk import build_gemini_client
from pydantic import TypeAdapter

SCOPE = Scope(tenant_id="t", account_id="a", principal_id="p", authorization_id="allowed")
MIB = 1024 * 1024
FONT = bytes(range(256)) * (3 * MIB // 256)  # 3 MiB, not UTF-8


def bundle(name: str, **files: bytes) -> SkillUpload:
    manifest = f"---\nname: {name}\ndescription: test\n---\nRead fonts/.\n".encode()
    return SkillUpload(
        display_title=name,
        files=(
            SkillUploadFile(path="SKILL.md", content=manifest),
            *(SkillUploadFile(path=path, content=body) for path, body in files.items()),
        ),
    )


LARGE = bundle("large", **{"fonts/f.ttf": FONT, "notes.md": b"plain"})
SMALL = bundle("small", **{"ref.md": b"text"})


class Files:
    """The host's bytes by skill version; `changed` serves other bytes than were pinned."""

    def __init__(self, *bundles: SkillUpload) -> None:
        self.by_version = {validate_bundle(b, upload=True): b for b in bundles}
        self.reads: list[SkillRef] = []
        self.changed: SkillUpload | None = None
        self.errors: list[ProviderError] = []

    async def __call__(self, scope: Scope, skill: SkillRef) -> SkillUpload:
        self.reads.append(skill)
        if self.errors:
            raise self.errors.pop(0)
        return self.changed or self.by_version[str(skill.version)]


def native(environment: str) -> Object:
    stamp = datetime.now(UTC).isoformat()
    return {
        "id": "i1",
        "status": "completed",
        "created": stamp,
        "updated": stamp,
        "environment_id": environment,
        "steps": [{"type": "model_output", "content": [{"type": "text", "text": "done"}]}],
    }


def driver(
    transport: FakeTransport, storage: MemoryStorage, files: Files | None
) -> GeminiManagedAgents:
    return GeminiManagedAgents(
        transport,
        storage=storage,
        state_store=MemoryStateStore(),
        account_scope_id="project",
        skill_files=files,
    )


async def pins(ma: GeminiManagedAgents, *bundles: SkillUpload) -> tuple[SkillRef, ...]:
    refs: list[SkillRef] = []
    for b in bundles:
        skill = await ma.skills.create(SCOPE, b, key=str(b.display_title))
        assert skill.latest_version is not None
        refs.append(skill.latest_version)
    return tuple(refs)


async def start(ma: GeminiManagedAgents, skills: tuple[SkillRef, ...]) -> Session:
    agent = await ma.agents.create(
        SCOPE,
        AgentSpec(name="a", model=ModelRef(provider="gemini", id="gemini-3.8-flash")),
        key="a",
    )
    spec = SessionSpec(
        agent=agent.ref,
        agent_revision=agent.revision,
        config_revision=1,
        skills=skills,
        extensions={
            "gemini.session": ExtensionConfig(
                namespace="gemini.session",
                version=1,
                value={
                    "thread": {
                        "channel": {"tenant_id": "t", "platform": "discord", "channel_id": "c"},
                        "thread_id": "th",
                    },
                    "binding_id": "b",
                },
            )
        },
    )
    return await ma.sessions.create(SCOPE, spec, key="s")


def targets(config: Object) -> list[str]:
    sources = config["sources"]
    assert isinstance(sources, list)
    return [str(s["target"]) for s in sources if isinstance(s, dict)]


async def send(ma: GeminiManagedAgents, transport: FakeTransport, session: Session, env: str):
    transport.responses.append(native(env))
    await ma.events.send(
        SCOPE, session.ref, (UserMessage(content=(TextPart(text="hi"),)),), key="t1"
    )
    return transport.requests[-1]


@pytest.mark.asyncio
async def test_a_large_binary_skill_is_uploaded_before_the_first_turn() -> None:
    transport, storage = FakeTransport(), MemoryStorage()
    files = Files(LARGE, SMALL)
    ma = driver(transport, storage, files)
    large, small = await pins(ma, LARGE, SMALL)
    async with storage.read() as records:
        record = records.skills[large.id]
        assert large.version in record.uploads and large.version not in record.bundles
        deployed = record.skill.native
        assert isinstance(deployed, dict) and deployed["deployment"] == "upload_on_session"
        assert small.version in records.skills[small.id].bundles
    session = await start(ma, (large, small))
    # Only its SKILL.md is a source (so the agent lists the skill); the rest uploads.
    ((env, config),) = transport.environments.items()
    assert f".agents/skills/{large.id}/SKILL.md" in targets(config)
    assert not any(t.startswith(f".agents/skills/{large.id}/fonts") for t in targets(config))
    assert f".agents/skills/{small.id}/ref.md" in targets(config)
    assert transport.uploads == [(env, f".agents/skills/{large.id}", True)]
    assert transport.environment_files[env] == {
        f".agents/skills/{large.id}/fonts/f.ttf": len(FONT),
        f".agents/skills/{large.id}/notes.md": 5,
    }
    assert session.native == {"skills_left_out": [], "created": True}
    request = await send(ma, transport, session, env)
    assert request["environment"] == env
    # The uploaded files are installed, never the agent's outputs.
    async with storage.read() as records:
        saved = records.sessions[session.ref.id].request["environment"]
        covered = mounted_files(saved, records.skills)
    assert covered.covers(f".agents/skills/{large.id}/fonts/f.ttf", sha256(FONT).hexdigest())
    assert not covered.covers(f".agents/skills/{large.id}/report.html", sha256(b"x").hexdigest())


@pytest.mark.asyncio
async def test_a_failed_upload_replaces_the_environment_without_that_skill() -> None:
    transport, storage = FakeTransport(), MemoryStorage()
    other = bundle("other", **{"img.png": FONT[: MIB + 5]})
    ma = driver(transport, storage, Files(LARGE, other, SMALL))
    large, kept, small = await pins(ma, LARGE, other, SMALL)
    transport.upload_errors.append(ProviderError("invalid_request", retryable=False))
    session = await start(ma, (large, kept, small))
    assert session.native == {"skills_left_out": [large.id], "created": True}
    first, second = transport.environments
    assert transport.deleted_environments == [first]
    assert f".agents/skills/{large.id}/SKILL.md" not in targets(transport.environments[second])
    assert f".agents/skills/{kept.id}/SKILL.md" in targets(transport.environments[second])
    assert f".agents/skills/{kept.id}/img.png" in transport.environment_files[second]
    request = await send(ma, transport, session, second)
    assert request["environment"] == second


@pytest.mark.asyncio
async def test_when_every_upload_fails_the_session_runs_on_todays_path() -> None:
    transport, storage = FakeTransport(), MemoryStorage()
    files = Files(LARGE, SMALL)
    files.changed = bundle("large", **{"fonts/f.ttf": FONT[:-1], "notes.md": b"plain"})
    ma = driver(transport, storage, files)
    large, small = await pins(ma, LARGE, SMALL)
    session = await start(ma, (large, small))
    # Bytes that differ from the pinned manifest never upload.
    assert transport.uploads == []
    assert session.native == {"skills_left_out": [large.id], "created": True}
    request = await send(ma, transport, session, "e1")
    environment = request["environment"]
    assert isinstance(environment, dict)
    assert f".agents/skills/{small.id}/SKILL.md" in targets(environment)
    assert not any(large.id in t for t in targets(environment))


class Crash(Exception):
    """A process dying mid-upload."""


@pytest.mark.asyncio
async def test_a_crashed_start_resumes_in_its_saved_environment() -> None:
    transport, storage = FakeTransport(), MemoryStorage()
    ma = driver(transport, storage, Files(LARGE))
    (large,) = await pins(ma, LARGE)
    real = transport.upload_archive

    async def crash(*args: object, **kwargs: object) -> None:
        raise Crash

    transport.upload_archive = crash  # type: ignore[method-assign]
    with pytest.raises(Crash):
        await start(ma, (large,))
    transport.upload_archive = real  # type: ignore[method-assign]
    (env,) = transport.environments
    async with storage.read() as records:
        (record,) = records.sessions.values()
        assert record.environment_id == env and record.uploading == (large.id,)
    with pytest.raises(ProviderError) as pending:
        await ma.events.send(
            SCOPE, record.session.ref, (UserMessage(content=(TextPart(text="hi"),)),), key="t"
        )
    assert pending.value.native_code == "skill_upload_pending" and pending.value.retryable
    # An earlier try left the same files: they are uploaded over, never trusted.
    transport.environment_files[env] = {
        f".agents/skills/{large.id}/fonts/f.ttf": len(FONT),
        f".agents/skills/{large.id}/notes.md": 5,
    }
    session = await start(ma, (large,))
    assert list(transport.environments) == [env]
    assert transport.uploads == [(env, f".agents/skills/{large.id}", True)]
    assert session.native == {"skills_left_out": [], "created": True}


@pytest.mark.asyncio
async def test_a_retryable_read_failure_makes_no_environment() -> None:
    transport, storage = FakeTransport(), MemoryStorage()
    files = Files(LARGE)
    ma = driver(transport, storage, files)
    (large,) = await pins(ma, LARGE)
    files.errors.append(ProviderError("transient_network", retryable=True))
    with pytest.raises(ProviderError) as busy:
        await start(ma, (large,))
    assert busy.value.retryable and not transport.environments
    session = await start(ma, (large,))
    assert len(transport.environments) == 1 and session.native == {
        "skills_left_out": [],
        "created": True,
    }


@pytest.mark.asyncio
async def test_files_left_by_an_earlier_try_are_overwritten() -> None:
    transport, storage = FakeTransport(), MemoryStorage()
    ma = driver(transport, storage, Files(LARGE))
    (large,) = await pins(ma, LARGE)
    real = transport.create_environment

    async def stale(config: Object) -> str:
        made = await real(config)
        transport.environment_files[made] = {f".agents/skills/{large.id}/notes.md": 1}
        return made

    transport.create_environment = stale  # type: ignore[method-assign]
    session = await start(ma, (large,))
    (env,) = transport.environments
    assert [u[2] for u in transport.uploads] == [True]
    assert transport.environment_files[env][f".agents/skills/{large.id}/notes.md"] == 5
    assert session.native == {"skills_left_out": [], "created": True}


@pytest.mark.asyncio
async def test_without_host_bytes_a_large_skill_stays_refused() -> None:
    ma = driver(FakeTransport(), MemoryStorage(), None)
    with pytest.raises(UnsupportedCapability):
        await ma.skills.create(SCOPE, LARGE, key="large")
    over = bundle("over", **{"a.bin": b"\0" * (28 * MIB)})
    with pytest.raises(UnsupportedCapability):
        validate_bundle(over, upload=True)


def test_the_archive_holds_only_checked_regular_files() -> None:
    uploaded = upload_manifest(LARGE)
    assert [path for path, _, _ in uploaded.files] == ["fonts/f.ttf", "notes.md"]
    archive = skill_archive(uploaded, LARGE)
    assert archive == skill_archive(uploaded, LARGE)  # deterministic
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
        members = tar.getmembers()
    assert [(m.name, m.size, m.isfile()) for m in members] == [
        ("fonts/f.ttf", len(FONT), True),
        ("notes.md", 5, True),
    ]
    changed = bundle("large", **{"fonts/f.ttf": FONT[::-1], "notes.md": b"plain"})
    with pytest.raises(ProviderError) as mismatch:
        skill_archive(uploaded, changed)
    assert mismatch.value.native_code == "skill_bytes_mismatch"


@pytest.mark.asyncio
async def test_records_saved_before_uploads_still_decode() -> None:
    transport, storage = FakeTransport(), MemoryStorage()
    ma = driver(transport, storage, Files(SMALL))
    await start(ma, await pins(ma, SMALL))
    adapter = TypeAdapter(Records)
    async with storage.read() as records:
        dumped = adapter.dump_python(records)
    for record in dumped["sessions"].values():
        del record["uploading"], record["left_out"]
    for record in dumped["skills"].values():
        del record["uploads"]
    assert adapter.validate_python(dumped) == records


class Gated(FakeTransport):
    """Holds each upload call (by arrival order) until its gate opens."""

    def __init__(self) -> None:
        super().__init__()
        self.arrived = 0
        self.gates: dict[int, asyncio.Event] = {}
        self.refused: dict[int, ProviderError] = {}

    async def upload_archive(
        self, environment_id: str, path: str, archive: bytes, *, overwrite: bool
    ) -> None:
        index, self.arrived = self.arrived, self.arrived + 1
        if index in self.gates:
            await self.gates[index].wait()
        if index in self.refused:
            raise self.refused[index]
        await super().upload_archive(environment_id, path, archive, overwrite=overwrite)


async def until(condition: Callable[[], bool]) -> None:
    for _ in range(5000):
        if condition():
            return
        await asyncio.sleep(0)
    raise AssertionError("never reached")


@pytest.mark.asyncio
async def test_interleaved_creates_never_leave_a_skill_mounted_without_its_files() -> None:
    transport, storage = Gated(), MemoryStorage()
    first, second = bundle("first", **{"a.bin": FONT}), bundle("second", **{"b.bin": FONT})
    ma = driver(transport, storage, Files(first, second))
    pin_first, pin_second = await pins(ma, first, second)
    for index in (0, 1, 3, 4):
        transport.gates[index] = asyncio.Event()
    transport.refused[0] = ProviderError("invalid_request", retryable=False)
    one = asyncio.create_task(start(ma, (pin_first, pin_second)))
    await until(lambda: transport.arrived == 1)  # one is uploading the first skill
    two = asyncio.create_task(start(ma, (pin_first, pin_second)))
    await until(lambda: transport.arrived == 2)  # two is uploading it into the same environment
    transport.gates[0].set()  # one: refused, leaves the first skill out, replaces the environment
    await until(lambda: transport.arrived == 4)
    transport.gates[1].set()  # two: its environment is gone
    for gate in transport.gates.values():
        gate.set()
    sessions = await asyncio.gather(one, two)
    assert sessions[0].ref == sessions[1].ref
    async with storage.read() as records:
        record = records.sessions[sessions[0].ref.id]
    assert record.left_out == (pin_first.id,) and record.uploading == ()
    assert record.environment_id is not None
    assert transport.environment_files[record.environment_id] == {
        f".agents/skills/{pin_second.id}/b.bin": len(FONT)
    }
    environment = transport.environments[record.environment_id]
    assert not any(pin_first.id in t for t in targets(environment))
    request = await send(ma, transport, sessions[0], record.environment_id)
    assert request["environment"] == record.environment_id


@pytest.mark.asyncio
async def test_a_vanished_environment_is_made_again_with_every_skill() -> None:
    transport, storage = FakeTransport(), MemoryStorage()
    ma = driver(transport, storage, Files(LARGE))
    (large,) = await pins(ma, LARGE)
    real = transport.upload_archive

    async def vanish(environment_id: str, *args: object, **kwargs: object) -> None:
        transport.upload_archive = real  # type: ignore[method-assign]
        del transport.environment_files[environment_id]
        await real(environment_id, *args, **kwargs)  # type: ignore[arg-type]

    transport.upload_archive = vanish  # type: ignore[method-assign]
    session = await start(ma, (large,))
    first, second = transport.environments
    assert session.native == {"skills_left_out": [], "created": True}
    assert f".agents/skills/{large.id}/fonts/f.ttf" in transport.environment_files[second]
    async with storage.read() as records:
        assert records.sessions[session.ref.id].environment_id == second
    assert first not in transport.environment_files


@pytest.mark.asyncio
async def test_a_refused_environment_falls_back_to_todays_path() -> None:
    transport, storage = FakeTransport(), MemoryStorage()
    ma = driver(transport, storage, Files(LARGE, SMALL))
    large, small = await pins(ma, LARGE, SMALL)

    async def refuse(config: Object) -> str:
        raise ProviderError("invalid_request", retryable=False)

    transport.create_environment = refuse  # type: ignore[method-assign]
    session = await start(ma, (large, small))
    assert session.native == {"skills_left_out": [large.id], "created": True}
    request = await send(ma, transport, session, "e1")
    environment = request["environment"]
    assert isinstance(environment, dict)
    assert f".agents/skills/{small.id}/SKILL.md" in targets(environment)
    assert not any(large.id in t for t in targets(environment))


@pytest.mark.asyncio
async def test_leaving_everything_out_drops_a_saved_environment() -> None:
    transport, storage = FakeTransport(), MemoryStorage()
    ma = driver(transport, storage, Files(LARGE))
    (large,) = await pins(ma, LARGE)
    real = transport.upload_archive

    async def crash(*args: object, **kwargs: object) -> None:
        raise Crash

    transport.upload_archive = crash  # type: ignore[method-assign]
    with pytest.raises(Crash):
        await start(ma, (large,))
    transport.upload_archive = real  # type: ignore[method-assign]
    (env,) = transport.environments
    # The host can no longer supply files: the saved environment would mount a bare SKILL.md.
    session = await start(driver(transport, storage, None), (large,))
    assert session.native == {"skills_left_out": [large.id], "created": True}
    assert transport.deleted_environments == [env]
    async with storage.read() as records:
        record = records.sessions[session.ref.id]
    assert record.environment_id is None and record.workspace_expires_at is None
    request = await send(ma, transport, session, "e1")
    assert request["environment"] == {"type": "remote"}


@pytest.mark.asyncio
async def test_an_unreadable_skill_is_left_out_before_any_environment_exists() -> None:
    transport, storage = FakeTransport(), MemoryStorage()
    files = Files(LARGE, SMALL)
    ma = driver(transport, storage, files)
    large, small = await pins(ma, LARGE, SMALL)
    files.errors.append(ProviderError("invalid_request", retryable=False))
    session = await start(ma, (large, small))
    assert session.native == {"skills_left_out": [large.id], "created": True}
    assert (
        transport.uploads == []
        and not transport.environments
        and not transport.deleted_environments
    )


@pytest.mark.asyncio
async def test_a_first_turn_in_a_missing_environment_is_continuity_lost() -> None:
    transport, storage = FakeTransport(), MemoryStorage()
    ma = driver(transport, storage, Files(LARGE))
    (large,) = await pins(ma, LARGE)
    session = await start(ma, (large,))
    transport.responses.append(ProviderError("not_found", retryable=False))
    with pytest.raises(ContinuityLost):
        await ma.events.send(
            SCOPE, session.ref, (UserMessage(content=(TextPart(text="hi"),)),), key="t1"
        )


@pytest.mark.asyncio
async def test_a_first_turn_never_mounts_an_uploaded_skill_without_its_environment() -> None:
    transport, storage = FakeTransport(), MemoryStorage()
    ma = driver(transport, storage, Files(LARGE))
    (large,) = await pins(ma, LARGE)
    session = await start(ma, (large,))
    async with storage.transaction() as records:
        records.sessions[session.ref.id].environment_id = None
    with pytest.raises(ProviderError) as refused:
        await ma.events.send(
            SCOPE, session.ref, (UserMessage(content=(TextPart(text="hi"),)),), key="t1"
        )
    assert refused.value.native_code == "skill_environment_missing"
    assert not transport.requests


@pytest.mark.asyncio
async def test_a_replayed_create_is_not_marked_new() -> None:
    transport, storage = FakeTransport(), MemoryStorage()
    ma = driver(transport, storage, Files(SMALL))
    (small,) = await pins(ma, SMALL)
    assert (await start(ma, (small,))).native == {"created": True}
    replay = await start(ma, (small,))
    native = replay.native
    assert not (isinstance(native, dict) and native.get("created")), "the replay is not a creation"


@pytest.mark.asyncio
async def test_a_catalog_agent_refuses_an_uploaded_version() -> None:
    storage = MemoryStorage()
    ma = driver(FakeTransport(), storage, Files(LARGE))
    (large,) = await pins(ma, LARGE)
    spec = AgentSpec(
        name="a", model=ModelRef(provider="gemini", id="gemini-3.8-flash"), skills=(large,)
    )
    async with storage.transaction() as records:
        with pytest.raises(UnsupportedCapability) as refused:
            native_definition(records, SCOPE, "project", spec, "agent-1")
    assert refused.value.missing == ("catalog_uploaded_skill",)


def raw(request: httpx.Request) -> str:
    return request.url.raw_path.decode().partition("?")[0]


@pytest.mark.asyncio
async def test_the_sdk_transport_speaks_the_environment_routes() -> None:
    seen: list[httpx.Request] = []

    def reply(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.method == "POST":
            return httpx.Response(200, json={"id": "env/1"})
        if request.method == "GET" and "page_token" not in request.url.params:
            return httpx.Response(
                200,
                json={
                    "files": [{"path": ".agents/skills/x/c.md", "type": "FILE", "size_bytes": 1}],
                    "next_page_token": "p2",
                },
            )
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "files": [
                        {
                            "path": "workspace/.agents/skills/x/a.bin",
                            "type": "FILE",
                            "size_bytes": "3",
                        },
                        {"path": ".agents/skills/x/b.md", "type": "FILE", "size_bytes": 2},
                        {"path": ".agents/skills/x/dir", "type": "DIRECTORY"},
                    ]
                },
            )
        return httpx.Response(200, json={})

    with build_gemini_client(
        transport=httpx.MockTransport(reply),
        api_key="offline-fixture",
        base_url="https://generativelanguage.googleapis.com",
    ) as client:
        async with client.aio:
            sdk = SDKTransport(client)
            env = await sdk.create_environment({"sources": [], "network": "disabled"})
            await sdk.upload_archive(env, ".agents/skills/x", b"gz", overwrite=True)
            listed = await sdk.list_files(env, ".agents/skills/x")
            await sdk.delete_environment(env)
    assert env == "env/1"
    assert listed == {"c.md": 1, "a.bin": 3, "b.md": 2}
    create, upload, files, more, delete = seen
    assert more.url.params["page_token"] == "p2"
    assert (create.method, create.url.path) == ("POST", "/v1beta/environments")
    assert json.loads(create.content) == {"sources": [], "network": "disabled"}
    assert upload.method == "PUT"
    assert raw(upload) == "/upload/v1beta/environments/env%2F1/files/.agents/skills/x/"
    assert dict(upload.url.params) == {"extract": "true", "overwrite": "true"}
    assert upload.content == b"gz"
    assert raw(files) == "/v1beta/environments/env%2F1/files/.agents/skills/x"
    assert files.url.params["recursive"] == "true"
    assert (delete.method, raw(delete)) == ("DELETE", "/v1beta/environments/env%2F1")
