"""Offline native bytes -> real Discord upload ownership observations.

An observation proves byte delivery, not generation. Authored tool text, MIME,
filenames and a fixture PDF cannot prove that a model generated this turn's PDF.
Generation remains PENDING until an independently owned native recording binding
exists. This module never constructs TurnEvidence or a complete catalog PASS.
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
from collections.abc import Awaitable, Callable
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING, Literal, Self, cast

import discord
from daimon.adapters.discord.output_delivery import AnswerMessage, deliver_provider_output
from daimon.core.media.filenames import display_filename_for
from daimon.core.output_delivery import MAX_BYTES_PER_FILE, DeliverableFile
from daimon.testing.discord_surface import OfflineDiscordGateway, SurfaceModel
from discord.http import MultipartParameters
from mux.contracts.ids import ResourceRef, Scope
from mux.contracts.ports import Artifacts
from mux.contracts.resources import Artifact
from pydantic import ConfigDict, Field, model_validator

if TYPE_CHECKING:
    from discord.types.message import Message as MessagePayload


class FilePin(SurfaceModel):
    path: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    def read(self, root: Path) -> bytes:
        path = (root / self.path).resolve()
        if not path.is_relative_to(root.resolve()) or not path.is_file():
            raise ValueError("artifact pin is not an owned file")
        if path.stat().st_size > MAX_BYTES_PER_FILE:
            raise ValueError("artifact recording exceeds delivery limit")
        content = path.read_bytes()
        if len(content) > MAX_BYTES_PER_FILE or digest(content) != self.sha256:
            raise ValueError("artifact recording changed")
        return content


def digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


class CapturedBytes(SurfaceModel):
    model_config = SurfaceModel.model_config | ConfigDict(
        ser_json_bytes="base64", val_json_bytes="base64"
    )
    content: bytes = Field(max_length=MAX_BYTES_PER_FILE)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @classmethod
    def capture(cls, content: bytes) -> Self:
        return cls(content=content, sha256=digest(content))

    @model_validator(mode="after")
    def actual_hash(self) -> Self:
        if digest(self.content) != self.sha256:
            raise ValueError("artifact hash does not describe captured bytes")
        return self


class NativeArtifactCapture(SurfaceModel):
    boundary: Literal["native_artifact_port_download"] = "native_artifact_port_download"
    scope: Scope
    session: ResourceRef
    root_turn_id: str = Field(min_length=1)
    artifact: Artifact
    tape: FilePin
    taped_body: FilePin
    native_bytes: CapturedBytes

    @model_validator(mode="after")
    def owned_artifact(self) -> Self:
        session, ref = self.session, self.artifact.ref
        if (
            session.kind != "session"
            or ref.kind not in {"artifact", "file"}
            or self.artifact.session != session
            or (ref.provider, ref.account_scope_id, ref.tenant_id, ref.account_id)
            != (session.provider, session.account_scope_id, session.tenant_id, session.account_id)
            or (session.tenant_id, session.account_id)
            != (self.scope.tenant_id, self.scope.account_id)
            or (self.artifact.turn_id is not None and self.artifact.turn_id != self.root_turn_id)
            or self.artifact.size_bytes != len(self.native_bytes.content)
            or self.taped_body.sha256 != self.native_bytes.sha256
            or not self.native_bytes.content
        ):
            raise ValueError("native artifact is not owned by this scoped turn")
        return self


async def capture_native_artifact(
    artifacts: Artifacts,
    scope: Scope,
    ref: ResourceRef,
    *,
    session: ResourceRef,
    root_turn_id: str,
    tape: FilePin,
    taped_body: FilePin,
    recording_root: Path,
) -> NativeArtifactCapture:
    """Read through the actual owner's artifact port, bounded by pinned tape bytes.

    This supports downloaded artifacts and Gemini's mounted snapshot members via
    its real artifact download implementation. It never executes authored bash or
    creates the claimed PDF. Caller-supplied pins are integrity, not generation
    authority. Missing bytes must be reported PENDING before calling this hook.
    """
    tape.read(recording_root)
    expected = taped_body.read(recording_root)
    if (
        ref.provider != session.provider
        or ref.account_scope_id != session.account_scope_id
        or (ref.tenant_id, ref.account_id) != (scope.tenant_id, scope.account_id)
        or (session.tenant_id, session.account_id) != (scope.tenant_id, scope.account_id)
        or not root_turn_id
    ):
        raise ValueError("foreign native artifact binding")
    artifact = await artifacts.retrieve(scope, ref)
    if artifact.ref != ref or artifact.session != session:
        raise ValueError("foreign artifact metadata")
    # Validate metadata before downloading or ever calling an adapter.
    NativeArtifactCapture(
        scope=scope,
        session=session,
        root_turn_id=root_turn_id,
        artifact=artifact,
        tape=tape,
        taped_body=taped_body,
        native_bytes=CapturedBytes.capture(expected),
    )
    contents = bytearray()
    source = artifacts.download(scope, ref)
    try:
        async for chunk in source:
            if len(contents) + len(chunk) > len(expected):
                raise ValueError("native download exceeds its recorded body")
            contents.extend(chunk)
    finally:
        close = getattr(source, "aclose", None)
        if close is not None:
            await close()
    actual = bytes(contents)
    if actual != expected:
        raise ValueError("native download differs from recorded artifact bytes")
    # Re-check sidecar and native tape after the await boundary.
    tape.read(recording_root)
    if taped_body.read(recording_root) != actual:
        raise ValueError("artifact recording changed during download")
    return NativeArtifactCapture(
        scope=scope,
        session=session,
        root_turn_id=root_turn_id,
        artifact=artifact,
        tape=tape,
        taped_body=taped_body,
        native_bytes=CapturedBytes.capture(actual),
    )


class UploadCapture(SurfaceModel):
    boundary: Literal["discord_multipart_accepted"] = "discord_multipart_accepted"
    evidence_id: str = Field(min_length=1)
    operation: Literal["send", "edit"]
    channel_id: str
    message_id: str
    attachment_id: str
    filename: str
    uploaded_bytes: CapturedBytes
    readback_bytes: CapturedBytes


class OutputUploadGateway(OfflineDiscordGateway):
    """Accepting offline Discord endpoint, capturing real multipart file streams.

    Only successfully acknowledged uploads are recorded. An attachment name,
    by-reference attachment, or method return without bytes is not an upload.
    """

    def __init__(self, *, evidence_id: str, channel_id: int, clock: Callable[[], float]) -> None:
        super().__init__(evidence_id=evidence_id, channel_id=channel_id, clock=clock)
        self.uploads: list[UploadCapture] = []

    async def close(self) -> None:
        """The offline endpoint owns no network connections."""

    async def _upload(
        self, channel_id: int, params: MultipartParameters, *, message_id: int | None = None
    ) -> MessagePayload:
        streams: list[tuple[str, bytes]] = []
        for file in params.files or ():
            position = file.fp.tell()
            body = file.fp.read(MAX_BYTES_PER_FILE + 1)
            file.fp.seek(position)
            if len(body) > MAX_BYTES_PER_FILE:
                raise ValueError("multipart artifact exceeds delivery limit")
            streams.append((file.filename, body))
        if message_id is None:
            response = await super().send_message(channel_id, params=params)
        else:
            response = await super().edit_message(channel_id, message_id, params=params)
        attachments = response.get("attachments", [])
        # Uploaded placeholders use indices 0..N-1 in the serialized request.
        # The base endpoint assigns fresh ids and unique byte-backed URLs to them.
        new = attachments[-len(streams) :] if streams else []
        if len(new) != len(streams):
            raise ValueError("Discord did not acknowledge every binary upload")
        for (name, body), attachment in zip(streams, new, strict=True):
            if attachment["filename"] != name or attachment["size"] != len(body):
                raise ValueError("Discord upload acknowledgement does not match binary request")
            readback = await self.get_from_cdn(attachment["url"])
            self.uploads.append(
                UploadCapture(
                    evidence_id=f"{self.evidence_id}:upload:{len(self.uploads)}",
                    operation="send" if message_id is None else "edit",
                    channel_id=str(channel_id),
                    message_id=str(response["id"]),
                    attachment_id=str(attachment["id"]),
                    filename=name,
                    uploaded_bytes=CapturedBytes.capture(body),
                    readback_bytes=CapturedBytes.capture(readback),
                )
            )
        return response

    async def send_message(self, channel_id: int, *, params: MultipartParameters) -> MessagePayload:
        return await self._upload(channel_id, params)

    async def edit_message(
        self, channel_id: int, message_id: int, *, params: MultipartParameters
    ) -> MessagePayload:
        return await self._upload(channel_id, params, message_id=message_id)


class PdfCheck(SurfaceModel):
    status: Literal["PASS", "FAIL", "PENDING"]
    code: Literal["PARSED_PDF", "NOT_PDF", "INVALID_PDF", "PDF_PARSER_UNAVAILABLE"]


def check_pdf(content: bytes) -> PdfCheck:
    """Require PDF magic AND qpdf's real object/xref/page-tree parse, offline.

    The parser is optional test tooling. An unavailable/timed-out parser gives
    PENDING, never PASS. Its diagnostics (which may contain document text) are
    never logged. No filename, extension, MIME or canned answer affects this.
    """
    if not content.startswith(b"%PDF-"):
        return PdfCheck(status="FAIL", code="NOT_PDF")
    parser = shutil.which("qpdf")
    if parser is None:
        return PdfCheck(status="PENDING", code="PDF_PARSER_UNAVAILABLE")
    with TemporaryDirectory(prefix="daimon-pdf-observation-") as directory:
        path = Path(directory) / "artifact.pdf"
        path.write_bytes(content)
        try:
            checked = subprocess.run(
                [parser, "--check", str(path)],
                capture_output=True,
                timeout=10,
                check=False,
            )
            if checked.returncode != 0:
                return PdfCheck(status="FAIL", code="INVALID_PDF")
            pages = subprocess.run(
                [parser, "--show-npages", str(path)],
                capture_output=True,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return PdfCheck(status="PENDING", code="PDF_PARSER_UNAVAILABLE")
        if pages.returncode != 0 or not pages.stdout.strip().isdigit() or int(pages.stdout) < 1:
            return PdfCheck(status="FAIL", code="INVALID_PDF")
    return PdfCheck(status="PASS", code="PARSED_PDF")


class PdfOwnershipObservation(SurfaceModel):
    native: NativeArtifactCapture
    adapter_evidence_id: str = Field(min_length=1)
    channel_id: str = Field(min_length=1)
    answer_message_id: str | None
    upload: UploadCapture | None
    pdf: PdfCheck
    # Generation requires separate native recording ownership, never a tag in
    # this envelope. D12's current authored tapes have no real artifact at all.
    generation_status: Literal["PENDING"] = "PENDING"
    generation_code: Literal["NATIVE_GENERATION_UNBOUND"] = "NATIVE_GENERATION_UNBOUND"

    @model_validator(mode="after")
    def bound_upload(self) -> Self:
        if self.pdf != check_pdf(self.native.native_bytes.content):
            raise ValueError("PDF verdict does not describe captured native bytes")
        upload = self.upload
        if upload is not None and (
            upload.channel_id != self.channel_id
            or not upload.evidence_id.startswith(self.adapter_evidence_id + ":upload:")
            or upload.filename
            != display_filename_for(self.native.artifact.filename, self.native.artifact.media_type)
            or (upload.operation == "edit" and upload.message_id != self.answer_message_id)
        ):
            raise ValueError("upload is not bound to this adapter answer")
        return self

    @property
    def delivery_status(self) -> Literal["PASS", "FAIL", "PENDING"]:
        if self.upload is None:
            return "FAIL"
        if self.upload.uploaded_bytes != self.native.native_bytes:
            return "FAIL"
        if self.upload.readback_bytes != self.native.native_bytes:
            return "FAIL"
        return self.pdf.status


async def observe_pdf_delivery(
    native: NativeArtifactCapture,
    thread: discord.Thread,
    gateway: OutputUploadGateway,
    *,
    may_post: Callable[[], Awaitable[bool]],
    answer: AnswerMessage | None = None,
) -> PdfOwnershipObservation:
    """Use #750 unchanged; capture the actual adapter upload and its readback.

    This is delivery evidence only. Consumers retain PENDING for generation and
    other D12 assertions (e.g. complete post history); never turn it into a PASS.
    """
    native = NativeArtifactCapture.model_validate_json(native.model_dump_json())
    if (
        thread.id != gateway.channel_id or cast(object, thread._state.http) is not gateway  # pyright: ignore[reportPrivateUsage]
    ):
        raise ValueError("upload observer is not this thread's actual HTTP endpoint")
    if answer is not None:
        current = await thread.fetch_message(answer.message_id)
        if current.channel.id != thread.id:
            raise ValueError("answer does not belong to the output thread")
    before = len(gateway.uploads)
    item = native.artifact
    delivered = await deliver_provider_output(
        DeliverableFile(
            file_id=item.ref.id,
            filename=item.filename,
            mime_type=item.media_type,
            size_bytes=len(native.native_bytes.content),
            content=native.native_bytes.content,
        ),
        thread,
        session_id=native.session.id,
        may_post=may_post,
        answer=answer,
    )
    added = gateway.uploads[before:]
    upload = added[0] if delivered and len(added) == 1 else None
    if upload is not None:
        if upload.filename != display_filename_for(item.filename, item.media_type):
            raise ValueError("adapter acknowledgement names a different artifact")
        if upload.channel_id != str(thread.id):
            raise ValueError("upload acknowledgement belongs to another channel")
        message = await thread.fetch_message(int(upload.message_id))
        match = next((a for a in message.attachments if str(a.id) == upload.attachment_id), None)
        if match is None or match.filename != upload.filename:
            raise ValueError("accepted artifact is absent from the actual message readback")
        readback = await match.read()
        upload = UploadCapture.model_validate_json(upload.model_dump_json()).model_copy(
            update={"readback_bytes": CapturedBytes.capture(readback)}
        )
    return PdfOwnershipObservation(
        native=native,
        adapter_evidence_id=gateway.evidence_id,
        channel_id=str(thread.id),
        answer_message_id=str(answer.message_id) if answer is not None else None,
        upload=upload,
        pdf=check_pdf(native.native_bytes.content),
    )


class D12CellAvailability(SurfaceModel):
    scenario_id: Literal["QA-D12-PDF-CLAIM-HAS-PDF"] = "QA-D12-PDF-CLAIM-HAS-PDF"
    backend: Literal["anthropic", "openai", "gemini"]
    status: Literal["PENDING"] = "PENDING"
    code: Literal["NATIVE_ARTIFACT_BYTES_MISSING"] = "NATIVE_ARTIFACT_BYTES_MISSING"
    reason: str
    source_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    source: FilePin


def d12_availability() -> tuple[D12CellAvailability, ...]:
    """Source-pinned cuts, NOT verdicts inferred from fabricated artifact fixtures.

    These exact published snapshots contain no mounted/downloaded artifact body.
    Any future recording/consumer must re-audit availability instead of crediting
    a generated control PDF, successful bash text, or the canned attachment claim.
    """
    native = FilePin(
        path="packages/testing/fixtures/target53/native/QA-D12-PDF-CLAIM-HAS-PDF.json",
        sha256="453f7cc144b5f064cdd8eb1990a3f54354ec7ec48007abb3c61c2a7662cbcbb7",
    )
    return (
        D12CellAvailability(
            backend="anthropic",
            source_commit="5787984757acc93ac15d4e8bd6e183769d01139b",
            source=FilePin(
                path="packages/testing/fixtures/target53/anthropic/QA-D12-PDF-CLAIM-HAS-PDF.json",
                sha256="c2692f32c1c65ede795789099a9d6902a41fe65b3959f93c6f3bdfd0bf8b20f4",
            ),
            reason=(
                "Authored bash/tool success and attachment claim contain no artifact body; "
                "no generation or upload proof."
            ),
        ),
        D12CellAvailability(
            backend="openai",
            source_commit="5787984757acc93ac15d4e8bd6e183769d01139b",
            source=native,
            reason=(
                "Pinned native recipe marks ARTIFACT_PROTOCOL_UNBOUND; "
                "no native artifact bytes or host upload."
            ),
        ),
        D12CellAvailability(
            backend="gemini",
            source_commit="5787984757acc93ac15d4e8bd6e183769d01139b",
            source=native,
            reason=(
                "Pinned native recipe marks ARTIFACT_PROTOCOL_UNBOUND; "
                "no mounted snapshot member or host upload."
            ),
        ),
    )
