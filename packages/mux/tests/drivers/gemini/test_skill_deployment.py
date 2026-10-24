"""Full authored default bytes, native GCS compilation and scoped pin failures."""

import json
import os
from collections.abc import Iterator, Mapping
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import NoReturn

import httpx
import pytest
import yaml
from mux.contracts.actions import UserMessage
from mux.contracts.events import TextPart
from mux.contracts.extensions import ExtensionConfig
from mux.contracts.ids import ModelRef, Scope, SkillRef
from mux.contracts.resources import AgentSpec, SessionSpec, SkillUpload, SkillUploadFile
from mux.drivers.gemini import GeminiManagedAgents
from mux.drivers.gemini.bundles import MAX_BUNDLE_BYTES, validate_bundle, validate_targets
from mux.drivers.gemini.skill_deployment import (
    GCSBundleDeployment,
    file_manifest,
    validate_deployment,
)
from mux.drivers.gemini.transport import SDKTransport, Transport
from mux.errors import OperationConflict, ProviderError, ScopeViolation, UnsupportedCapability
from mux.state.memory import MemoryStateStore
from mux.testing.drivers.gemini.fake import FakeTransport, MemoryStorage
from mux.testing.isolated_sdk import AmbientSDKConfigurationError, build_gemini_client
from pydantic import BaseModel, JsonValue

SCOPE = Scope(tenant_id="t", account_id="a", principal_id="p", authorization_id="allowed")
ROOT = Path(__file__).resolve().parents[5]

SDK_CONFIGS = (
    ("offline-fixture", "https://generativelanguage.googleapis.com"),
    ("offline-f2-fixture", "https://gemini.invalid"),
)


class NamesWithoutValues(Mapping[str, str]):
    """Provider presence must refuse before even reading a synthetic value."""

    def __init__(self, name: str) -> None:
        self.name = name

    def __iter__(self) -> Iterator[str]:
        return iter((self.name,))

    def __len__(self) -> int:
        return 1

    def __getitem__(self, key: str) -> str:
        raise AssertionError("SDK construction read an ambient value")


@pytest.mark.parametrize(("api_key", "base_url"), SDK_CONFIGS)
@pytest.mark.parametrize(
    "name", ["GOOGLE_API_KEY", "GEMINI_API_KEY", "GOOGLE_CLOUD_PROJECT", "GOOGLE_FUTURE_ROUTE"]
)
def test_replay_sdk_refuses_initial_provider_names_before_allocation(
    api_key: str, base_url: str, name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(*args: object, **kwargs: object) -> NoReturn:
        raise AssertionError("ambient configuration reached SDK/HTTP client allocation")

    transport = httpx.MockTransport(lambda _: httpx.Response(500))
    # Restore the stand-in before pytest updates its own environment metadata.
    with monkeypatch.context() as isolated:
        isolated.setattr(os, "environ", NamesWithoutValues(name))
        isolated.setattr("google.genai.Client", forbidden)
        isolated.setattr(httpx, "Client", forbidden)
        isolated.setattr(httpx, "AsyncClient", forbidden)
        with pytest.raises(AmbientSDKConfigurationError) as error:
            build_gemini_client(transport=transport, api_key=api_key, base_url=base_url)
        if name == "GOOGLE_FUTURE_ROUTE":
            assert name not in str(error.value)


@pytest.mark.parametrize(("api_key", "base_url"), SDK_CONFIGS)
async def test_replay_sdk_owns_sync_async_models_and_interactions(
    api_key: str, base_url: str
) -> None:
    requests: list[httpx.Request] = []

    def reply(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={"models": []}
            if request.url.path.endswith("/models")
            else {"id": "owned", "status": "completed", "outputs": []},
        )

    with build_gemini_client(
        transport=httpx.MockTransport(reply), api_key=api_key, base_url=base_url
    ) as client:
        async with client.aio:
            list(client.models.list())
            await client.aio.models.list()
            client.interactions.get("owned")
            await client.aio.interactions.get("owned")
    assert len(requests) == 4
    assert all(request.url.host == httpx.URL(base_url).host for request in requests)
    assert all(request.headers["x-goog-api-key"] == api_key for request in requests)
    assert all("authorization" not in request.headers for request in requests)


class DefaultSkill(BaseModel):
    type: str
    skill_id: str
    platforms: tuple[str, ...] | None = None


class AuthoredDefault(BaseModel):
    skills: tuple[DefaultSkill, ...]


@pytest.fixture
def defaults() -> dict[str, SkillUpload]:
    authored = AuthoredDefault.model_validate(
        yaml.safe_load((ROOT / "defaults/agents/daimon.yaml").read_text())
    )
    return {
        skill.skill_id: SkillUpload(
            display_title=skill.skill_id,
            files=tuple(
                SkillUploadFile(
                    path=path.relative_to(directory).as_posix(), content=path.read_bytes()
                )
                for path in sorted(directory.rglob("*"))
                if path.is_file()
            ),
        )
        for skill in authored.skills
        if skill.type == "custom" and skill.platforms is None
        for directory in (ROOT / "defaults/skills" / skill.skill_id,)
    }


class OfflinePublisher:
    """Scripted storage adapter, explicitly not evidence of a live deployment."""

    def __init__(self) -> None:
        self.calls: list[tuple[Scope, str, str, SkillUpload]] = []
        self.contents: dict[str, dict[str, bytes]] = {}
        self.receipt_changes: dict[str, object] = {}

    async def publish(
        self, scope: Scope, *, account_scope_id: str, version: str, bundle: SkillUpload
    ) -> GCSBundleDeployment:
        self.calls.append((scope, account_scope_id, version, bundle))
        source = f"gs://offline-bundles/{scope.tenant_id}/{scope.account_id}/{version}/"
        self.contents[source] = {file.path: file.content for file in bundle.files}
        receipt = GCSBundleDeployment(
            scope.tenant_id,
            scope.account_id,
            account_scope_id,
            version,
            source,
            file_manifest(bundle),
        )
        return replace(receipt, **self.receipt_changes)


def driver(
    transport: Transport, storage: MemoryStorage, publisher: OfflinePublisher | None
) -> GeminiManagedAgents:
    return GeminiManagedAgents(
        transport,
        storage=storage,
        state_store=MemoryStateStore(),
        account_scope_id="project",
        skill_bundle_publisher=publisher,
    )


async def session(ma: GeminiManagedAgents, pins: tuple[SkillRef, ...], *, key: str = "s") -> None:
    agent = await ma.agents.create(
        SCOPE,
        AgentSpec(
            name="full-default",
            model=ModelRef(provider="gemini", id="gemini-3.8-flash"),
            skills=pins,
        ),
        key=f"agent-{key}",
    )
    s = await ma.sessions.create(
        SCOPE,
        SessionSpec(
            agent=agent.ref,
            agent_revision=agent.revision,
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
        key=key,
    )
    await ma.events.send(SCOPE, s.ref, (UserMessage(content=(TextPart(text="hello"),)),), key=key)


@pytest.mark.asyncio
async def test_all_eleven_full_default_bundles_reach_sdk_as_pinned_gcs_directories(
    defaults: dict[str, SkillUpload],
) -> None:
    publisher, storage = OfflinePublisher(), MemoryStorage()
    captured: list[httpx.Request] = []
    stamp = datetime.now(UTC).isoformat()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "generativelanguage.googleapis.com"
        assert request.headers["x-goog-api-key"] == "offline-fixture"
        captured.append(request)
        return httpx.Response(
            200,
            json={
                "id": "i1",
                "status": "completed",
                "created": stamp,
                "updated": stamp,
                "steps": [],
                "environment_id": "e1",
            },
        )

    with build_gemini_client(
        transport=httpx.MockTransport(handler),
        api_key="offline-fixture",
        base_url="https://generativelanguage.googleapis.com",
    ) as client:
        async with client.aio:
            ma = driver(SDKTransport(client), storage, publisher)
            pins: list[SkillRef] = []
            for name, bundle in defaults.items():
                skill = await ma.skills.create(SCOPE, bundle, key=name)
                assert skill.latest_version is not None
                pins.append(skill.latest_version)
            assert len(pins) == len(publisher.calls) == 11
            assert captured == []  # Native Interactions I/O starts at the turn.
            assert (
                sum(len(file.content) for file in defaults["pymc-artifact-style"].files)
                > MAX_BUNDLE_BYTES
            )
            expected: list[JsonValue] = []
            for pin, (scope, account, version, original) in zip(pins, publisher.calls, strict=True):
                assert scope == SCOPE and account == "project" and version == pin.version
                uri = f"gs://offline-bundles/t/a/{version}/"
                directory = ROOT / "defaults/skills" / str(original.display_title)
                assert publisher.contents[uri] == {
                    path.relative_to(directory).as_posix(): path.read_bytes()
                    for path in directory.rglob("*")
                    if path.is_file()
                }
                expected.append(
                    {"type": "gcs", "source": uri, "target": f".agents/skills/{pin.id}"}
                )
            # Restart without a publisher: persisted immutable mount receipts suffice.
            restarted = driver(SDKTransport(client), storage, None)
            await session(restarted, tuple(pins))
            assert len(captured) == 1
            assert captured[0].method == "POST"
            assert json.loads(captured[0].content)["environment"] == {
                "type": "remote",
                "sources": expected,
            }
            assert b'"type": "inline"' not in captured[0].content
            assert len(publisher.calls) == 11


@pytest.mark.asyncio
async def test_publish_pins_old_bytes_and_idempotent_replay_does_not_republish(
    defaults: dict[str, SkillUpload],
) -> None:
    publisher, transport, storage = OfflinePublisher(), FakeTransport(), MemoryStorage()
    ma = driver(transport, storage, publisher)
    original = defaults["pymc-artifact-style"]
    skill = await ma.skills.create(SCOPE, original, key="create")
    assert skill.latest_version is not None
    assert await ma.skills.create(SCOPE, original, key="create") == skill
    changed = original.model_copy(
        update={"files": (*original.files, SkillUploadFile(path="new.bin", content=b"\xff"))}
    )
    new = await ma.skills.publish_version(SCOPE, skill.id, changed, key="publish")
    assert new.ref.version != skill.latest_version.version
    assert await ma.skills.publish_version(SCOPE, skill.id, changed, key="publish") == new
    assert len(publisher.calls) == 2
    for key, pin in (("old", skill.latest_version), ("new", new.ref)):
        stamp = datetime.now(UTC).isoformat()
        transport.responses.append(
            {"id": key, "status": "completed", "created": stamp, "steps": []}
        )
        await session(driver(transport, storage, None), (pin,), key=key)
    old_source = f"gs://offline-bundles/t/a/{skill.latest_version.version}/"
    new_source = f"gs://offline-bundles/t/a/{new.ref.version}/"
    assert "new.bin" not in publisher.contents[old_source]
    assert publisher.contents[new_source]["new.bin"] == b"\xff"
    for request, source in zip(transport.requests, (old_source, new_source), strict=True):
        assert request["environment"] == {
            "type": "remote",
            "sources": [
                {"type": "gcs", "source": source, "target": f".agents/skills/{skill.id}"},
            ],
        }
    with pytest.raises(OperationConflict):
        await ma.skills.create(SCOPE, changed, key="create")
    with pytest.raises(ScopeViolation):
        await ma.skills.publish_version(
            SCOPE.model_copy(update={"tenant_id": "foreign"}), skill.id, changed, key="foreign"
        )
    assert len(publisher.calls) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changes, error",
    [
        ({"tenant_id": "foreign"}, ScopeViolation),
        ({"account_id": "foreign"}, ScopeViolation),
        ({"account_scope_id": "foreign"}, ScopeViolation),
        ({"version": "stale"}, ProviderError),
        ({"files": ()}, ProviderError),
        ({"source": "https://example.invalid/latest/"}, ValueError),
        ({"source": "gs://bucket/latest/"}, ValueError),
    ],
)
async def test_bad_publisher_receipt_never_acknowledges_a_skill(
    defaults: dict[str, SkillUpload],
    changes: dict[str, object],
    error: type[Exception],
) -> None:
    publisher, storage, transport = OfflinePublisher(), MemoryStorage(), FakeTransport()
    publisher.receipt_changes = changes
    ma = driver(transport, storage, publisher)
    with pytest.raises(error):
        await ma.skills.create(SCOPE, defaults["pymc-artifact-style"], key="bad")
    async with storage.transaction() as records:
        assert not records.skills and not records.creations
    assert transport.requests == []


@pytest.mark.asyncio
async def test_unconfigured_full_default_is_a_typed_dependency_before_io(
    defaults: dict[str, SkillUpload],
) -> None:
    ma = driver(FakeTransport(), MemoryStorage(), None)
    with pytest.raises(UnsupportedCapability, match="skill_bundle_gcs_deployment"):
        await ma.skills.create(SCOPE, defaults["pymc-artifact-style"], key="no-publisher")


@pytest.mark.parametrize(
    "sources",
    [
        [{"type": "inline", "target": "a", "content": "a" * (1024 * 1024 + 1)}],
        [{"type": "inline", "target": str(i), "content": "é" * (512 * 1024)} for i in range(3)],
    ],
)
def test_real_inline_byte_limits_apply_to_combined_mounts(sources: list[JsonValue]) -> None:
    with pytest.raises(UnsupportedCapability, match="skill_bundle_gcs_deployment"):
        validate_targets(sources)


def test_large_external_bundle_keeps_path_and_manifest_validation() -> None:
    with pytest.raises(ValueError, match="canonical"):
        validate_bundle(
            SkillUpload(files=(SkillUploadFile(path="./SKILL.md", content=b"x"),)), external=True
        )
    with pytest.raises(ValueError, match="traversal"):
        validate_bundle(
            SkillUpload(files=(SkillUploadFile(path="../SKILL.md", content=b"x"),)), external=True
        )


@pytest.mark.parametrize(
    "source",
    [
        "gs://user:secret@bucket/{version}/",
        "gs://bucket/{version}/?token=secret",
        "gs://bucket/{version}/#secret",
        "gs://bucket/../{version}/",
        "gs://bucket/%2e%2e/{version}/",
        "gs://bucket/\n/{version}/",
    ],
)
def test_publisher_uri_refuses_credentials_and_ambiguous_paths(source: str) -> None:
    bundle = SkillUpload(files=(SkillUploadFile(path="SKILL.md", content=b"# skill"),))
    version = validate_bundle(bundle)
    receipt = GCSBundleDeployment(
        "t", "a", "project", version, source.format(version=version), file_manifest(bundle)
    )
    with pytest.raises(ValueError):
        validate_deployment(receipt, SCOPE, "project", version, bundle)


@pytest.mark.asyncio
async def test_persisted_receipt_tampering_refuses_before_native_io(
    defaults: dict[str, SkillUpload],
) -> None:
    publisher, storage, transport = OfflinePublisher(), MemoryStorage(), FakeTransport()
    ma = driver(transport, storage, publisher)
    skill = await ma.skills.create(SCOPE, defaults["pymc-artifact-style"], key="create")
    assert skill.latest_version is not None and skill.latest_version.version is not None
    version = skill.latest_version.version
    async with storage.transaction() as records:
        prior = records.skills[skill.id].deployments[version]
        records.skills[skill.id].deployments[version] = replace(
            prior, files=(("SKILL.md", "wrong-hash"),)
        )
    with pytest.raises(ProviderError, match="skill_deployment_mismatch"):
        await session(driver(transport, storage, None), (skill.latest_version,))
    assert not transport.requests


@pytest.mark.asyncio
async def test_publisher_failure_rolls_back_without_acknowledgement(
    defaults: dict[str, SkillUpload],
) -> None:
    class FailingPublisher(OfflinePublisher):
        async def publish(
            self, scope: Scope, *, account_scope_id: str, version: str, bundle: SkillUpload
        ) -> GCSBundleDeployment:
            raise ProviderError("upstream", retryable=False)

    storage, transport = MemoryStorage(), FakeTransport()
    ma = driver(transport, storage, FailingPublisher())
    with pytest.raises(ProviderError):
        await ma.skills.create(SCOPE, defaults["pymc-artifact-style"], key="failed")
    async with storage.transaction() as records:
        assert not records.skills and not records.creations
    assert not transport.requests


@pytest.mark.parametrize(
    "files",
    [
        (SkillUploadFile(path="SKILL.md", content=b"\xff"),),
        (
            SkillUploadFile(path="SKILL.md", content=b"ok"),
            SkillUploadFile(path="a", content=b"x"),
            SkillUploadFile(path="a/b", content=b"y"),
        ),
    ],
)
def test_external_bundles_reject_invalid_skill_text_and_file_directory_collisions(
    files: tuple[SkillUploadFile, ...],
) -> None:
    with pytest.raises(ValueError):
        validate_bundle(SkillUpload(files=files), external=True)


@pytest.mark.parametrize(
    "defect",
    [
        None,
        "unlocked",
        "expired",
        "hold_only",
        "missing",
        "generation",
        "bytes",
        "extra",
        "repeated_cursor",
    ],
)
async def test_retained_storage_is_verified_live_with_locked_retention(monkeypatch, defect):
    from hashlib import sha256

    from mux.drivers.gemini.skill_deployment import verify_retained_deployments

    version = "a" * 64
    name = f"skills/{version}/SKILL.md"
    body = b"original skill bytes"
    receipt = GCSBundleDeployment(
        "t",
        "a",
        "project",
        version,
        f"gs://owned/skills/{version}/",
        (("SKILL.md", sha256(body).hexdigest()),),
    )
    calls = []

    def handle(call):
        calls.append(call)
        assert call.method == "GET" and call.url.host == "storage.googleapis.com"
        assert call.headers["authorization"] == "Bearer explicit-offline-read-only"
        if call.url.params.get("alt") == "media":
            assert call.url.params["generation"] == "123"
            return httpx.Response(200, content=b"changed" if defect == "bytes" else body)
        item = {
            "name": name,
            "bucket": "owned",
            "generation": "123",
            "retention": {"mode": "Locked", "retainUntilTime": "2026-10-18T00:00:00Z"},
        }
        if defect == "unlocked":
            item["retention"]["mode"] = "Unlocked"
        elif defect == "expired":
            item["retention"]["retainUntilTime"] = "2026-10-15T00:00:00Z"
        elif defect == "hold_only":
            del item["retention"]
            item["temporaryHold"] = True
        elif defect == "generation":
            item["generation"] = None
        result = {"items": [] if defect == "missing" else [item]}
        if defect == "extra":
            result["items"].append({**item, "name": name + ".extra"})
        elif defect == "repeated_cursor":
            result["items"] = []
            result["nextPageToken"] = "same"
        return httpx.Response(200, json=result)

    original = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: original(transport=httpx.MockTransport(handle), **kwargs),
    )
    if defect:
        with pytest.raises(ProviderError, match="skill_deployment_readback_mismatch"):
            await verify_retained_deployments(
                (receipt,),
                token="explicit-offline-read-only",
                retained_until=datetime(2026, 10, 17, tzinfo=UTC),
            )
    else:
        await verify_retained_deployments(
            (receipt,),
            token="explicit-offline-read-only",
            retained_until=datetime(2026, 10, 17, tzinfo=UTC),
        )
        assert len(calls) == 2
