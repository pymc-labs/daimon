from __future__ import annotations

import email
import io
import json
import logging
import zipfile
from collections.abc import AsyncIterator
from email import policy

import httpx
import pytest
from mux.contracts.ids import PageRequest, ResourceRef, Revision, Scope
from mux.contracts.resources import CredentialBinding, SkillUpload, SkillUploadFile
from mux.drivers.openai import artifacts, skills, transport, vaults
from mux.drivers.openai._common import Context
from mux.drivers.openai.transport import Object
from mux.errors import ProviderError, ScopeViolation, UnsupportedCapability
from openai import AsyncOpenAI

SCOPE = Scope(
    tenant_id="tenant", account_id="account", principal_id="human", authorization_id="grant"
)
SESSION = ResourceRef(
    id="session",
    kind="session",
    provider="openai",
    account_scope_id="project",
    tenant_id="tenant",
    account_id="account",
)
VAULT = SESSION.model_copy(update={"id": "vault", "kind": "vault"})


class Wire:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.uploads: list[bytes] = []
        self.interrupted = False
        self.foreign = False
        self.resolve_calls: list[tuple[Scope, str]] = []

    async def secret(self, scope: Scope, id_: str) -> str:
        self.resolve_calls.append((scope, id_))
        return "fictional-sentinel-secret"

    def artifact(self, id_: str) -> Object:
        return {
            "id": id_,
            "session_id": "session",
            "environment_id": "environment",
            "path": "/workspace/outputs/" + id_ + ".bin",
            "size_bytes": 256,
            "created_at": 0,
            "turn_id": "root",
        }

    async def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        assert request.headers["OpenAI-Beta"] == "agents=v1"
        if request.method in ("POST", "DELETE"):
            assert request.headers["Idempotency-Key"]
        path = request.url.path.removeprefix("/v1")
        if request.method == "POST" and path in ("/files", "/skills", "/skills/skill/versions"):
            msg = email.message_from_bytes(
                ("Content-Type: " + request.headers["content-type"] + "\r\n\r\n").encode()
                + request.content,
                policy=policy.default,
            )
            parts = list(msg.iter_parts())
            files = [p for p in parts if p.get_filename()]
            payload = files[0].get_payload(decode=True)
            assert isinstance(payload, bytes)
            self.uploads.append(payload)
            if path == "/files":
                assert files[0].get_filename() == "binary.bin"
                assert any(
                    p.get_payload(decode=True) == b"user_data"
                    for p in parts
                    if not p.get_filename()
                )
                return httpx.Response(
                    200,
                    json={"id": "input", "filename": "binary.bin", "bytes": 256, "created_at": 0},
                )
            with zipfile.ZipFile(io.BytesIO(self.uploads[-1])) as archive:
                assert archive.namelist() == ["bundle/SKILL.md", "bundle/data.bin"]
                assert archive.read("bundle/data.bin") == bytes(range(256))
            if path.endswith("/versions"):
                return httpx.Response(
                    200,
                    json={
                        "id": "native-version",
                        "skill_id": "skill",
                        "version": "2",
                        "name": "fixture",
                        "description": "sample",
                        "created_at": 0,
                    },
                )
            return httpx.Response(
                200,
                json={
                    "id": "skill",
                    "latest_version": "1",
                    "default_version": "1",
                    "name": "fixture",
                    "description": "sample",
                    "created_at": 0,
                },
            )
        if path == "/agents/sessions/session":
            return httpx.Response(
                200,
                json={
                    "id": "session",
                    "metadata": {"mux_tenant": "foreign" if self.foreign else "tenant"},
                },
            )
        if path == "/agents/sessions/session/artifacts":
            assert "turn_id" not in request.url.params
            second = bool(request.url.params.get("after"))
            id_ = "second" if second else "first"
            return httpx.Response(
                200, json={"data": [self.artifact(id_)], "has_more": not second, "last_id": id_}
            )
        if path.startswith("/agents/sessions/session/artifacts/"):
            id_ = path.split("/")[5]
            if path.endswith("/content"):
                if self.interrupted:
                    return httpx.Response(200, content=bytes(range(17)))
                return httpx.Response(200, content=bytes(range(256)))
            return httpx.Response(200, json=self.artifact(id_))
        if path == "/files/input/content":
            return httpx.Response(200, content=bytes(range(256)))
        if path == "/files/input":
            return httpx.Response(
                200, json={"id": "input", "filename": "binary.bin", "bytes": 256, "created_at": 0}
            )
        if path == "/vaults/vault":
            return httpx.Response(
                200,
                json={
                    "id": "vault",
                    "name": "shared",
                    "metadata": {
                        "mux_tenant": "foreign" if self.foreign else "tenant",
                        "mux_account": "account",
                    },
                    "created_at": 0,
                },
            )
        if path == "/vaults/vault/credentials" and request.method == "POST":
            body = json.loads(request.content)
            assert body["auth"]["token"] == "fictional-sentinel-secret"
            assert body["auth"]["mcp_server_url"] == "https://mcp.invalid/tool"
            return httpx.Response(
                200,
                json={
                    "id": "credential",
                    "vault_id": "vault",
                    "created_at": 0,
                    "name": body["name"],
                    "auth": {
                        "type": "static_bearer",
                        "mcp_server_url": "https://mcp.invalid/tool",
                        "token": "fictional-sentinel-secret",
                    },
                    "unknown_secret": "fictional-sentinel-secret",
                },
            )
        return httpx.Response(404, json={"error": {"message": "fixture"}})

    def ports(
        self,
    ) -> tuple[AsyncOpenAI, skills.OpenAISkills, artifacts.OpenAIArtifacts, vaults.OpenAIVaults]:
        sdk = AsyncOpenAI(
            api_key="offline-fixture",
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(self.handle)),
        )
        c = Context(
            transport.SDKTransport(sdk),
            "project",
            "openai.persistent_workspace",
            lambda s, k, i: s == SCOPE,
        )
        return (
            sdk,
            skills.OpenAISkills(c),
            artifacts.OpenAIArtifacts(c),
            vaults.OpenAIVaults(c, self.secret),
        )


async def binary() -> AsyncIterator[bytes]:
    yield bytes(range(128))
    yield bytes(range(128, 256))


def bundle() -> SkillUpload:
    return SkillUpload(
        files=(
            SkillUploadFile(
                path="SKILL.md",
                content=(
                    b"---\nname: fixture\ndescription: sample\n---\nRun the offline fixture.\n"
                ),
            ),
            SkillUploadFile(path="data.bin", content=bytes(range(256))),
        )
    )


@pytest.mark.asyncio
async def test_exact_inline_skill_bundle_and_explicit_immutable_version() -> None:
    wire = Wire()
    sdk, port, _, _ = wire.ports()
    record = await port.create(SCOPE, bundle(), key="create")
    assert record.latest_version is not None
    assert record.latest_version.id == "skill" and record.latest_version.version == "1"
    version = await port.publish_version(SCOPE, "skill", bundle(), key="publish")
    assert version.ref.id == "skill" and version.version == version.ref.version == "2"
    assert len(wire.uploads) == 2
    await sdk.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    ["../SKILL.md", "/SKILL.md", "nested/SKILL.md", "a\\SKILL.md", "a/../SKILL.md", "a//SKILL.md"],
)
async def test_invalid_bundle_paths_refused_before_io(path: str) -> None:
    wire = Wire()
    sdk, port, _, _ = wire.ports()
    with pytest.raises(ProviderError):
        await port.create(
            SCOPE,
            SkillUpload(
                files=(
                    SkillUploadFile(
                        path=path,
                        content=(
                            b"---\nname: fixture\ndescription: sample\n---\n"
                            b"Run the offline fixture.\n"
                        ),
                    ),
                )
            ),
            key="invalid",
        )
    assert not wire.requests
    await sdk.close()


@pytest.mark.asyncio
async def test_binary_artifact_pages_exact_bytes_and_truncation_is_typed_failure() -> None:
    wire = Wire()
    sdk, _, port, _ = wire.ports()
    first = await port.list(SCOPE, SESSION, page=PageRequest(limit=1))
    second = await port.list(SCOPE, SESSION, page=PageRequest(limit=1, cursor=first.next_cursor))
    assert len(first.data) == len(second.data) == 1 and first.has_more and not second.has_more
    assert first.data[0].ref.id != second.data[0].ref.id
    assert b"".join([x async for x in port.download(SCOPE, first.data[0].ref)]) == bytes(range(256))
    wire.interrupted = True
    with pytest.raises(ProviderError) as error:
        _ = b"".join([x async for x in port.download(SCOPE, first.data[0].ref)])
    assert error.value.category == "transient_network"
    await sdk.close()


@pytest.mark.asyncio
async def test_uploaded_input_file_is_distinct_and_preserves_all_binary_bytes() -> None:
    wire = Wire()
    sdk, _, port, _ = wire.ports()
    file = await port.upload(
        SCOPE, binary(), filename="binary.bin", media_type="application/octet-stream", key="upload"
    )
    assert (
        file.ref.id == "file:input" and file.session is None and wire.uploads == [bytes(range(256))]
    )
    assert b"".join([x async for x in port.download(SCOPE, file.ref)]) == bytes(range(256))
    await sdk.close()


@pytest.mark.asyncio
async def test_foreign_parent_stops_download_before_artifact_io() -> None:
    wire = Wire()
    sdk, _, port, _ = wire.ports()
    page = await port.list(SCOPE, SESSION, page=PageRequest())
    wire.foreign = True
    wire.requests.clear()
    with pytest.raises(ScopeViolation):
        _ = b"".join([x async for x in port.download(SCOPE, page.data[0].ref)])
    assert len(wire.requests) == 1 and wire.requests[0].url.path == "/v1/agents/sessions/session"
    await sdk.close()


@pytest.mark.asyncio
async def test_credentials_resolve_by_reference_and_echoed_secrets_never_enter_records() -> None:
    wire = Wire()
    sdk, _, _, port = wire.ports()
    record = await port.put(
        SCOPE,
        VAULT,
        CredentialBinding(
            name="mcp",
            kind="static_bearer",
            credential_ref="host-ref",
            mcp_server_url="https://mcp.invalid/tool",
        ),
        expected=None,
        key="put",
    )
    assert wire.resolve_calls == [(SCOPE, "host-ref")]
    assert (
        record.kind == "static_bearer"
        and "fictional-sentinel-secret" not in repr(record)
        and "fictional-sentinel-secret" not in record.model_dump_json()
    )
    await sdk.close()


@pytest.mark.asyncio
async def test_foreign_vault_refuses_before_secret_resolution() -> None:
    wire = Wire()
    sdk, _, _, port = wire.ports()
    wire.foreign = True
    with pytest.raises(ScopeViolation):
        await port.put(
            SCOPE,
            VAULT,
            CredentialBinding(
                name="mcp",
                kind="static_bearer",
                credential_ref="host-ref",
                mcp_server_url="https://mcp.invalid/tool",
            ),
            expected=None,
            key="put",
        )
    assert not wire.resolve_calls and len(wire.requests) == 1
    await sdk.close()


@pytest.mark.asyncio
async def test_conditional_credential_write_refuses_before_any_io() -> None:
    wire = Wire()
    sdk, _, _, port = wire.ports()
    with pytest.raises(UnsupportedCapability):
        await port.put(
            SCOPE,
            VAULT,
            CredentialBinding(
                name="mcp",
                kind="static_bearer",
                credential_ref="host-ref",
                mcp_server_url="https://mcp.invalid/tool",
            ),
            expected=Revision(local=1),
            key="put",
        )
    assert not wire.requests and not wire.resolve_calls
    await sdk.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["create", "publish"])
async def test_separate_display_title_refused_before_upload(method: str) -> None:
    wire = Wire()
    sdk, port, _, _ = wire.ports()
    try:
        labeled = bundle().model_copy(update={"display_title": "different title"})
        with pytest.raises(UnsupportedCapability):
            if method == "create":
                await port.create(SCOPE, labeled, key="labeled")
            else:
                await port.publish_version(SCOPE, "skill", labeled, key="labeled")
        assert wire.requests == []
    finally:
        await sdk.close()


@pytest.mark.asyncio
async def test_ensure_detects_same_name_across_native_pages_without_creating() -> None:
    wire = Wire()

    async def handle(request: httpx.Request) -> httpx.Response:
        wire.requests.append(request)
        second = bool(request.url.params.get("after"))
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": "vault2" if second else "vault1",
                        "name": "shared",
                        "metadata": {"mux_tenant": "tenant", "mux_account": "account"},
                    }
                ],
                "has_more": not second,
                "last_id": "vault2" if second else "vault1",
            },
        )

    wire.handle = handle
    sdk, _, _, port = wire.ports()
    try:
        with pytest.raises(ProviderError) as error:
            await port.ensure(SCOPE, "shared", key="ensure")
        assert error.value.category == "conflict"
        assert len(wire.requests) == 2 and all(r.method == "GET" for r in wire.requests)
    finally:
        await sdk.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("uploaded", [False, True])
async def test_deletion_targets_one_owned_input_or_session_artifact(uploaded: bool) -> None:
    wire = Wire()
    sdk, _, port, _ = wire.ports()
    try:
        if uploaded:
            artifact = await port.upload(
                SCOPE,
                binary(),
                filename="binary.bin",
                media_type="application/octet-stream",
                key="upload",
            )
            expected_path = "/v1/files/input"
        else:
            page = await port.list(SCOPE, SESSION, page=PageRequest())
            artifact = page.data[0]
            expected_path = "/v1/agents/sessions/session/artifacts/first"
        wire.requests.clear()
        receipt = await port.delete(SCOPE, artifact.ref, key="delete-artifact")
        assert receipt.deleted == (artifact.ref,)
        deletes = [r for r in wire.requests if r.method == "DELETE"]
        assert len(deletes) == 1 and deletes[0].url.path == expected_path
        assert deletes[0].headers["Idempotency-Key"] == "delete-artifact"
    finally:
        await sdk.close()


@pytest.mark.asyncio
async def test_foreign_native_parent_blocks_artifact_delete() -> None:
    wire = Wire()
    sdk, _, port, _ = wire.ports()
    try:
        page = await port.list(SCOPE, SESSION, page=PageRequest())
        wire.foreign = True
        wire.requests.clear()
        with pytest.raises(ScopeViolation):
            await port.delete(SCOPE, page.data[0].ref, key="delete-foreign")
        assert len(wire.requests) == 1 and wire.requests[0].method == "GET"
    finally:
        await sdk.close()


@pytest.mark.asyncio
async def test_real_sdk_debug_options_do_not_log_the_credential(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG, logger="openai._base_client")
    wire = Wire()
    sdk, _, _, port = wire.ports()
    try:
        await port.put(
            SCOPE,
            VAULT,
            CredentialBinding(
                name="mcp",
                kind="static_bearer",
                credential_ref="host-ref",
                mcp_server_url="https://mcp.invalid/tool",
            ),
            expected=None,
            key="put",
        )
        assert "Request options:" in caplog.text and "[redacted]" in caplog.text
        assert "fictional-sentinel-secret" not in caplog.text
        # Wire.handle independently requires the exact resolved token in JSON.
        assert any(r.method == "POST" for r in wire.requests)
    finally:
        await sdk.close()
