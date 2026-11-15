"""Skills past the inline limits: uploaded into an environment made before the first turn."""

import io
import json
import tarfile
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
from mux.drivers.gemini.transport import Object, SDKTransport
from mux.errors import ProviderError, UnsupportedCapability
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
    assert transport.uploads == [(env, f".agents/skills/{large.id}", False)]
    assert transport.environment_files[env] == {
        f".agents/skills/{large.id}/fonts/f.ttf": len(FONT),
        f".agents/skills/{large.id}/notes.md": 5,
    }
    assert session.native == {"skills_left_out": []}
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
    assert session.native == {"skills_left_out": [large.id]}
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
    assert session.native == {"skills_left_out": [large.id]}
    request = await send(ma, transport, session, "e1")
    environment = request["environment"]
    assert isinstance(environment, dict)
    assert f".agents/skills/{small.id}/SKILL.md" in targets(environment)
    assert not any(large.id in t for t in targets(environment))


@pytest.mark.asyncio
async def test_a_crashed_start_resumes_in_its_saved_environment() -> None:
    transport, storage = FakeTransport(), MemoryStorage()
    files = Files(LARGE)
    files.errors.append(ProviderError("transient_network", retryable=True))
    ma = driver(transport, storage, files)
    (large,) = await pins(ma, LARGE)
    with pytest.raises(ProviderError):
        await start(ma, (large,))
    (env,) = transport.environments
    async with storage.read() as records:
        (record,) = records.sessions.values()
        assert record.environment_id == env and record.uploading == (large.id,)
    with pytest.raises(ProviderError) as pending:
        await ma.events.send(
            SCOPE, record.session.ref, (UserMessage(content=(TextPart(text="hi"),)),), key="t"
        )
    assert pending.value.native_code == "skill_upload_pending" and pending.value.retryable
    # An earlier try left the same files: the 409 counts as uploaded once sizes match.
    transport.environment_files[env] = {
        f".agents/skills/{large.id}/fonts/f.ttf": len(FONT),
        f".agents/skills/{large.id}/notes.md": 5,
    }
    session = await start(ma, (large,))
    assert list(transport.environments) == [env]
    assert transport.uploads == [(env, f".agents/skills/{large.id}", False)]
    assert session.native == {"skills_left_out": []}


@pytest.mark.asyncio
async def test_other_bytes_left_by_an_earlier_try_are_overwritten() -> None:
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
    assert [u[2] for u in transport.uploads] == [False, True]
    assert transport.environment_files[env][f".agents/skills/{large.id}/notes.md"] == 5
    assert session.native == {"skills_left_out": []}


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
