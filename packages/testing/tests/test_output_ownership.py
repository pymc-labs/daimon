"""Protocol controls only: none of these PDFs credit D12 model generation."""

from __future__ import annotations

import io
import json
import shutil
import tarfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import httpx
import pytest
from daimon.adapters.discord.output_delivery import AnswerMessage
from daimon.testing.discord_surface import DiscordSurfaceHarness
from daimon.testing.drivers.gemini.fake import FakeTransport, MemoryStorage
from daimon.testing.isolated_sdk import build_async_anthropic, build_async_openai
from daimon.testing.output_ownership import (
    CapturedBytes,
    FilePin,
    NativeArtifactCapture,
    OutputUploadGateway,
    PdfOwnershipObservation,
    capture_native_artifact,
    check_pdf,
    d12_availability,
    digest,
    observe_pdf_delivery,
)
from discord.http import HTTPClient
from mux.contracts.actions import UserMessage
from mux.contracts.events import TextPart
from mux.contracts.extensions import ExtensionConfig
from mux.contracts.ids import ModelRef, PageRequest, Provider, ResourceRef, Scope
from mux.contracts.ports import Artifacts
from mux.contracts.resources import AgentSpec, EnvironmentSpec, SessionSpec
from mux.drivers.anthropic.resources._authorization import ResourceAuthorization
from mux.drivers.anthropic.resources.artifacts import AnthropicArtifacts
from mux.drivers.gemini import GeminiManagedAgents
from mux.drivers.openai._common import Context
from mux.drivers.openai.artifacts import OpenAIArtifacts, artifact_identity
from mux.drivers.openai.transport import SDKTransport
from mux.state.memory import MemoryStateStore
from pydantic import ValidationError

SCOPE = Scope(tenant_id="t", account_id="a", principal_id="p", authorization_id="grant")


def pdf_control() -> bytes:
    """Valid binary parser control, generated here, never in a scenario tape."""
    text = b"BT /F1 12 Tf 72 720 Td (QA PDF control) Tj ET"
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length " + str(len(text)).encode() + b" >>\nstream\n" + text + b"\nendstream",
    ]
    body = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for n, value in enumerate(objects, 1):
        offsets.append(len(body))
        body.extend(f"{n} 0 obj\n".encode() + value + b"\nendobj\n")
    xref = len(body)
    body.extend(f"xref\n0 {len(offsets)}\n0000000000 65535 f \n".encode())
    for offset in offsets[1:]:
        body.extend(f"{offset:010} 00000 n \n".encode())
    body.extend(
        f"trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    )
    return bytes(body)


def authorized(scope: Scope, kind: str, resource_id: str | None) -> bool:
    return scope == SCOPE and kind in {"session", "artifact"} and bool(resource_id)


def unavailable_parser(name: str) -> None:
    return None


def session_ref(provider: Provider) -> ResourceRef:
    return ResourceRef(
        id="session",
        kind="session",
        provider=provider,
        account_scope_id="project",
        tenant_id="t",
        account_id="a",
    )


def pin(root: Path, name: str, body: bytes) -> FilePin:
    (root / name).write_bytes(body)
    return FilePin(path=name, sha256=digest(body))


@asynccontextmanager
async def native_port(
    provider: Provider, body: bytes
) -> AsyncIterator[tuple[Artifacts, ResourceRef, ResourceRef, str, list[str]]]:
    """Use real driver decoders/download implementations; providers are offline."""
    calls: list[str] = []
    session = session_ref(provider)
    if provider == "anthropic":

        def wire(request: httpx.Request) -> httpx.Response:
            calls.append(request.url.path)
            if request.url.path == "/v1/files/file-qa":
                return httpx.Response(
                    200,
                    json={
                        "id": "file-qa",
                        "filename": "qa.pdf",
                        "mime_type": "application/pdf",
                        "size_bytes": len(body),
                        "created_at": "2026-10-11T00:00:00Z",
                        "downloadable": True,
                        "type": "file",
                        "scope": {"type": "session", "id": "session"},
                    },
                )
            if request.url.path == "/v1/files/file-qa/content":
                return httpx.Response(200, content=body)
            raise AssertionError("unexpected native artifact request")

        ref = session.model_copy(update={"kind": "file", "id": "file-qa"})
        auth = ResourceAuthorization(SCOPE, frozenset({("file", ref.id)}))
        async with build_async_anthropic(transport=httpx.MockTransport(wire)) as client:
            yield (
                AnthropicArtifacts(client, "project", authorization=auth),
                session,
                ref,
                "root",
                calls,
            )
    elif provider == "openai":

        def wire(request: httpx.Request) -> httpx.Response:
            calls.append(request.url.path)
            route = request.url.path.removeprefix("/v1")
            if route == "/agents/sessions/session":
                return httpx.Response(200, json={"id": "session", "metadata": {"mux_tenant": "t"}})
            if route == "/agents/sessions/session/artifacts/pdf":
                return httpx.Response(
                    200,
                    json={
                        "id": "pdf",
                        "session_id": "session",
                        "path": "/workspace/outputs/qa.pdf",
                        "size_bytes": len(body),
                        "created_at": 0,
                        "turn_id": "root",
                    },
                )
            if route == "/agents/sessions/session/artifacts/pdf/content":
                return httpx.Response(200, content=body)
            raise AssertionError("unexpected native artifact request")

        ref = session.model_copy(
            update={"kind": "artifact", "id": artifact_identity("session", "pdf")}
        )
        async with build_async_openai(transport=httpx.MockTransport(wire)) as client:
            context = Context(
                SDKTransport(client), "project", "openai.persistent_workspace", authorized
            )
            yield OpenAIArtifacts(context), session, ref, "root", calls
    else:
        transport = FakeTransport()
        ma = GeminiManagedAgents(
            transport,
            storage=MemoryStorage(),
            state_store=MemoryStateStore(),
            account_scope_id="project",
        )
        agent = await ma.agents.create(
            SCOPE,
            AgentSpec(name="control", model=ModelRef(provider="gemini", id="gemini-3.8-flash")),
            key="agent",
        )
        env = await ma.environments.create(SCOPE, EnvironmentSpec(name="control"), key="env")
        created = await ma.sessions.create(
            SCOPE,
            SessionSpec(
                agent=agent.ref,
                agent_revision=agent.revision,
                environment=env.ref,
                config_revision=1,
                extensions={
                    "gemini.session": ExtensionConfig(
                        namespace="gemini.session",
                        version=1,
                        value={
                            "thread": {
                                "channel": {
                                    "tenant_id": "t",
                                    "platform": "discord",
                                    "channel_id": "102",
                                },
                                "thread_id": "qa",
                            },
                            "binding_id": "control",
                        },
                    )
                },
            ),
            key="session",
        )
        # Gemini workspaces expire seven days after creation: stamp it now.
        stamp = datetime.now(UTC).isoformat()
        transport.responses.append(
            {
                "id": "interaction-control",
                "status": "completed",
                "created": stamp,
                "updated": stamp,
                "environment_id": "workspace-control",
                "steps": [],
                "usage": None,
            }
        )
        await ma.events.send(
            SCOPE, created.ref, (UserMessage(content=(TextPart(text="control"),)),), key="send"
        )
        archive = io.BytesIO()
        with tarfile.open(fileobj=archive, mode="w:gz") as tar:
            member = tarfile.TarInfo("outputs/qa.pdf")
            member.size = len(body)
            tar.addfile(member, io.BytesIO(body))
        transport.snapshots["workspace-control"] = archive.getvalue()
        files = await ma.artifacts.list(SCOPE, created.ref, page=PageRequest())
        item = files.data[0]
        assert item.turn_id is not None
        yield ma.artifacts, created.ref, item.ref, item.turn_id, transport.snapshot_reads


def upload_harness() -> tuple[DiscordSurfaceHarness, OutputUploadGateway]:
    harness = DiscordSurfaceHarness(
        evidence_id="pdf-control", session_id="session", root_turn_id="root"
    )
    gateway = OutputUploadGateway(
        evidence_id="pdf-control", channel_id=harness.thread.id, clock=lambda: 1.0
    )
    harness.client.http = cast(HTTPClient, gateway)
    harness.thread._state.http = cast(HTTPClient, gateway)  # pyright: ignore[reportPrivateUsage]
    return harness, gateway


async def allowed() -> bool:
    return True


async def denied() -> bool:
    return False


async def capture_control(
    root: Path, provider: Provider = "openai", body: bytes | None = None
) -> NativeArtifactCapture:
    content = body if body is not None else pdf_control()
    tape = pin(
        root, "native-control.json", b'{"provenance":"authored-sdk-wire","test_control":true}'
    )
    data = pin(root, "recorded-control-body.bin", content)
    async with native_port(provider, content) as (port, session, ref, turn, _):
        return await capture_native_artifact(
            port,
            SCOPE,
            ref,
            session=session,
            root_turn_id=turn,
            tape=tape,
            taped_body=data,
            recording_root=root,
        )


@pytest.mark.parametrize("provider", ["anthropic", "openai", "gemini"])
@pytest.mark.parametrize("on_answer", [True, False])
async def test_real_native_download_and_adapter_multipart_bytes_never_credit_generation(
    tmp_path: Path, provider: Provider, on_answer: bool
) -> None:
    native = await capture_control(tmp_path, provider)
    harness, gateway = upload_harness()
    try:
        original = await harness.thread.send("Attached qa.pdf.")
        answer = (
            AnswerMessage(message_id=original.id, edit=harness.transport.edit)
            if on_answer
            else None
        )
        observed = await observe_pdf_delivery(
            native, harness.thread, gateway, may_post=allowed, answer=answer
        )
        assert observed.upload is not None
        assert observed.upload.operation == ("edit" if on_answer else "send")
        assert (
            observed.native.native_bytes
            == observed.upload.uploaded_bytes
            == observed.upload.readback_bytes
        )
        assert (
            observed.upload.message_id == str(original.id)
            if on_answer
            else observed.upload.message_id != str(original.id)
        )
        assert observed.delivery_status == ("PASS" if shutil.which("qpdf") else "PENDING")
        assert observed.generation_status == "PENDING"
        assert PdfOwnershipObservation.model_validate_json(observed.model_dump_json()) == observed
        assert NativeArtifactCapture.model_validate_json(native.model_dump_json()) == native
        assert len(gateway.uploads) == 1
    finally:
        await harness.client.close()


@pytest.mark.parametrize(
    "body", [b"Attached qa.pdf.", b"\x89PNG\r\n", b"%PDF-1.4\nnot real PDF objects\n%%EOF"]
)
async def test_claim_extension_mime_and_magic_are_not_pdf_proof(
    tmp_path: Path, body: bytes
) -> None:
    native = await capture_control(tmp_path, body=body)
    harness, gateway = upload_harness()
    try:
        observation = await observe_pdf_delivery(native, harness.thread, gateway, may_post=allowed)
        assert observation.upload is not None
        assert observation.delivery_status != "PASS"
        assert observation.generation_status == "PENDING"
    finally:
        await harness.client.close()


async def test_permission_refusal_does_not_invent_an_upload(tmp_path: Path) -> None:
    native = await capture_control(tmp_path)
    harness, gateway = upload_harness()
    try:
        observed = await observe_pdf_delivery(native, harness.thread, gateway, may_post=denied)
        assert observed.upload is None and observed.delivery_status == "FAIL"
        assert gateway.uploads == [] and gateway.events == []
    finally:
        await harness.client.close()


@pytest.mark.parametrize("field", ["tenant_id", "account_id", "account_scope_id", "provider"])
async def test_foreign_reference_refused_before_native_or_adapter_io(
    tmp_path: Path, field: str
) -> None:
    data = pin(tmp_path, "body", pdf_control())
    tape = pin(tmp_path, "tape", b"control")
    async with native_port("openai", pdf_control()) as (port, session, ref, root, calls):
        with pytest.raises(ValueError, match="foreign"):
            await capture_native_artifact(
                port,
                SCOPE,
                ref.model_copy(update={field: "gemini" if field == "provider" else "foreign"}),
                session=session,
                root_turn_id=root,
                tape=tape,
                taped_body=data,
                recording_root=tmp_path,
            )
        assert calls == []


async def test_wrong_turn_refuses_download_before_adapter_io(tmp_path: Path) -> None:
    data = pin(tmp_path, "body", pdf_control())
    tape = pin(tmp_path, "tape", b"control")
    async with native_port("openai", pdf_control()) as (port, session, ref, _, calls):
        with pytest.raises(ValueError, match="owned"):
            await capture_native_artifact(
                port,
                SCOPE,
                ref,
                session=session,
                root_turn_id="another-root",
                tape=tape,
                taped_body=data,
                recording_root=tmp_path,
            )
        assert not any(call.endswith("/content") for call in calls)


@pytest.mark.parametrize(
    "different",
    [b"", b"short", pdf_control() + b"extra", pdf_control().replace(b"control", b"forgery")],
)
async def test_native_download_must_equal_actual_tape_bytes(
    tmp_path: Path, different: bytes
) -> None:
    data = pin(tmp_path, "body", different)
    tape = pin(tmp_path, "tape", b"control")
    async with native_port("openai", pdf_control()) as (port, session, ref, root, _):
        with pytest.raises(ValueError):
            await capture_native_artifact(
                port,
                SCOPE,
                ref,
                session=session,
                root_turn_id=root,
                tape=tape,
                taped_body=data,
                recording_root=tmp_path,
            )


@pytest.mark.parametrize(
    "mutation",
    [
        "native-hash",
        "upload-hash",
        "readback-hash",
        "channel",
        "message",
        "filename",
        "evidence",
        "generation",
    ],
)
async def test_json_roundtrip_preserves_byte_and_ownership_binding(
    tmp_path: Path, mutation: str
) -> None:
    native = await capture_control(tmp_path)
    harness, gateway = upload_harness()
    try:
        message = await harness.thread.send("answer")
        observed = await observe_pdf_delivery(
            native,
            harness.thread,
            gateway,
            may_post=allowed,
            answer=AnswerMessage(message_id=message.id, edit=harness.transport.edit),
        )
        raw = json.loads(observed.model_dump_json())
        if mutation == "native-hash":
            raw["native"]["native_bytes"]["sha256"] = "0" * 64
        elif mutation == "upload-hash":
            raw["upload"]["uploaded_bytes"]["sha256"] = "0" * 64
        elif mutation == "readback-hash":
            raw["upload"]["readback_bytes"]["sha256"] = "0" * 64
        elif mutation == "channel":
            raw["upload"]["channel_id"] = "foreign"
        elif mutation == "message":
            raw["upload"]["message_id"] = "foreign"
        elif mutation == "filename":
            raw["upload"]["filename"] = "other.pdf"
        elif mutation == "evidence":
            raw["upload"]["evidence_id"] = "foreign:upload:0"
        else:
            raw["generation_status"] = "PASS"
        with pytest.raises(ValidationError):
            PdfOwnershipObservation.model_validate_json(json.dumps(raw))
    finally:
        await harness.client.close()


def test_changed_or_escaped_binary_pin_refuses(tmp_path: Path) -> None:
    bound = pin(tmp_path, "body", pdf_control())
    (tmp_path / "body").write_bytes(b"changed")
    with pytest.raises(ValueError, match="changed"):
        bound.read(tmp_path)
    with pytest.raises(ValueError, match="owned"):
        FilePin(path="../outside", sha256="0" * 64).read(tmp_path)


def test_pdf_parser_unavailable_is_pending(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", unavailable_parser)
    assert check_pdf(pdf_control()).status == "PENDING"
    assert check_pdf(b"Attached qa.pdf").status == "FAIL"


def test_source_pinned_d12_cells_have_no_generation_or_upload_proof() -> None:
    cells = d12_availability()
    assert [cell.backend for cell in cells] == ["anthropic", "openai", "gemini"]
    assert all(
        cell.status == "PENDING" and cell.code == "NATIVE_ARTIFACT_BYTES_MISSING" for cell in cells
    )
    assert all(len(cell.source.sha256) == 64 and len(cell.source_commit) == 40 for cell in cells)
    assert all("proof" in cell.reason or "UNBOUND" in cell.reason for cell in cells)
    assert all(type(cell).model_validate_json(cell.model_dump_json()) == cell for cell in cells)


def test_captured_binary_hash_is_recomputed_not_a_declared_fixture_property() -> None:
    raw = CapturedBytes.capture(pdf_control()).model_dump(mode="json")
    raw["sha256"] = "0" * 64
    with pytest.raises(ValidationError):
        CapturedBytes.model_validate_json(json.dumps(raw))


async def test_no_new_binary_ack_cannot_reuse_stale_upload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    native = await capture_control(tmp_path)
    harness, gateway = upload_harness()
    try:
        first = await observe_pdf_delivery(native, harness.thread, gateway, may_post=allowed)
        assert first.upload is not None

        async def fake_return(*args: object, **kwargs: object) -> bool:
            return True

        monkeypatch.setattr("daimon.testing.output_ownership.deliver_provider_output", fake_return)
        second = await observe_pdf_delivery(native, harness.thread, gateway, may_post=allowed)
        assert second.upload is None and second.delivery_status == "FAIL"
        assert len(gateway.uploads) == 1
    finally:
        await harness.client.close()


async def test_cdn_readback_mutation_is_not_delivery_parity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    native = await capture_control(tmp_path)
    harness, gateway = upload_harness()
    try:

        async def different_bytes(url: str) -> bytes:
            return pdf_control().replace(b"control", b"forgery")

        monkeypatch.setattr(gateway, "get_from_cdn", different_bytes)
        result = await observe_pdf_delivery(native, harness.thread, gateway, may_post=allowed)
        assert result.upload is not None
        assert result.upload.uploaded_bytes == result.native.native_bytes
        assert result.upload.readback_bytes != result.native.native_bytes
        assert result.delivery_status == "FAIL"
    finally:
        await harness.client.close()


async def test_serialized_pdf_pass_cannot_override_actual_invalid_bytes(tmp_path: Path) -> None:
    native = await capture_control(tmp_path, body=b"Attached qa.pdf.")
    harness, gateway = upload_harness()
    try:
        result = await observe_pdf_delivery(native, harness.thread, gateway, may_post=allowed)
        raw = json.loads(result.model_dump_json())
        raw["pdf"] = {"status": "PASS", "code": "PARSED_PDF"}
        with pytest.raises(ValidationError, match="PDF verdict"):
            PdfOwnershipObservation.model_validate_json(json.dumps(raw))
    finally:
        await harness.client.close()


async def test_another_gateway_cannot_capture_thread_uploads(tmp_path: Path) -> None:
    native = await capture_control(tmp_path)
    harness, gateway = upload_harness()
    foreign = OutputUploadGateway(
        evidence_id="foreign", channel_id=harness.thread.id, clock=lambda: 1.0
    )
    try:
        with pytest.raises(ValueError, match="actual HTTP"):
            await observe_pdf_delivery(native, harness.thread, foreign, may_post=allowed)
        assert gateway.uploads == [] and foreign.uploads == []
    finally:
        await harness.client.close()
