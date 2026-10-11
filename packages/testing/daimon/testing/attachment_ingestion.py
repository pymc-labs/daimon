"""Actual Discord attachment bytes and offline native mount protocol controls.

Production hands PDF URLs to the agent. A separately driven SDK mount control
does not prove that a catalog turn fetched that URL or executed a workspace tool.
All catalog cells therefore remain typed PENDING; model-authored FILE text never
supplies workspace evidence. This module creates neither TurnEvidence nor roots.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal, Self

import discord
from daimon.testing.discord_surface import SurfaceModel
from daimon.testing.ma_sessions import FakeSessionsState
from daimon.testing.output_ownership import CapturedBytes, FilePin
from daimon.testing.provider_fixtures import ScenarioFixture
from mux.contracts.ids import ResourceRef, Scope
from mux.contracts.resources import ResourceBinding
from pydantic import Field, model_validator

type AttachmentScenario = Literal["QA-NEW1-PDF-BARE-MENTION-NO-DROP", "QA-D1-CANARY-TWO-TURN"]


class AttachmentBytes(SurfaceModel):
    boundary: Literal["discord_attachment_read"] = "discord_attachment_read"
    message_id: str = Field(pattern=r"^[0-9]+$")
    channel_id: str = Field(pattern=r"^[0-9]+$")
    attachment_id: str = Field(pattern=r"^[0-9]+$")
    filename: str = Field(min_length=1)
    url: str = Field(min_length=1)
    source: FilePin
    original_payload_json: str
    downloaded: CapturedBytes

    @model_validator(mode="after")
    def original_identity(self) -> Self:
        payload = json.loads(self.original_payload_json)
        expected = {
            "id": self.attachment_id,
            "filename": self.filename,
            "url": self.url,
            "size": len(self.downloaded.content),
        }
        rows = payload.get("attachments", [])
        matches = [row for row in rows if str(row.get("id")) == self.attachment_id]
        if (
            str(payload.get("id")) != self.message_id
            or str(payload.get("channel_id")) != self.channel_id
            or len(matches) != 1
            or self.filename != Path(self.source.path).name
            or any(matches[0].get(key) != value for key, value in expected.items())
            or self.source.sha256 != self.downloaded.sha256
            or not self.downloaded.content.startswith(b"%PDF-")
        ):
            raise ValueError("download is not the pinned original Discord PDF")
        return self


async def read_attachment(
    message: discord.Message,
    *,
    attachment_id: int,
    original_payload_json: str,
    source: FilePin,
    catalog_root: Path,
) -> AttachmentBytes:
    """Download through discord.py's actual Attachment.read, never a body argument.

    The caller supplies its owned offline CDN HTTP endpoint. This read is an
    observation control: the production PDF path itself performs no eager read.
    """
    expected = source.read(catalog_root)
    attachment = next((a for a in message.attachments if a.id == attachment_id), None)
    if attachment is None or attachment.size != len(expected):
        raise ValueError("original PDF attachment is missing or has a foreign size")

    # Check the original identity before HTTP, even when callers swap Message objects.
    def capture(content: bytes) -> AttachmentBytes:
        return AttachmentBytes(
            message_id=str(message.id),
            channel_id=str(message.channel.id),
            attachment_id=str(attachment.id),
            filename=attachment.filename,
            url=attachment.url,
            source=source,
            original_payload_json=original_payload_json,
            downloaded=CapturedBytes.capture(content),
        )

    capture(expected)
    actual = await attachment.read(use_cached=False)
    if actual != expected or source.read(catalog_root) != expected:
        raise ValueError("CDN bytes differ from the frozen PDF")
    return capture(actual)


class MountControl(SurfaceModel):
    boundary: Literal["offline_native_mount_materializer"] = "offline_native_mount_materializer"
    scenario_cell_credit: Literal[False] = False
    scope: Scope
    session: ResourceRef
    binding: ResourceBinding
    attachment: AttachmentBytes
    mount_path: str = Field(min_length=1)
    workspace_bytes: CapturedBytes

    @model_validator(mode="after")
    def owned_bytes(self) -> Self:
        file = self.binding.resource
        native = self.binding.native
        if (
            self.scope.is_platform
            or self.scope.is_legacy_host_authorized
            or self.session.kind != "session"
            or self.session.provider != "anthropic"
            or (self.session.tenant_id, self.session.account_id)
            != (self.scope.tenant_id, self.scope.account_id)
            or file is None
            or self.binding.kind != "artifact"
            or file.kind != "file"
            or (file.provider, file.account_scope_id, file.tenant_id, file.account_id)
            != (
                self.session.provider,
                self.session.account_scope_id,
                self.session.tenant_id,
                self.session.account_id,
            )
            or native is None
            or native.namespace != "anthropic.session_resource"
            or native.value.get("id") != self.binding.id
            or native.value.get("file_id") != file.id
            or native.value.get("mount_path") != self.mount_path
            or self.workspace_bytes.content != self.attachment.downloaded.content
        ):
            raise ValueError("mount control lacks the actual scoped file/byte identity")
        return self


def capture_mount_control(
    attachment: AttachmentBytes,
    *,
    scope: Scope,
    session: ResourceRef,
    binding: ResourceBinding,
    state: FakeSessionsState,
) -> MountControl:
    """Read the existing SDK fake's mount materializer, without writing its state.

    The caller must actually upload and mount via the native driver first. This
    reads the independently materialized byte copy, not a fixture-declared PDF or
    a model reply. Fake sandbox readback is explicitly not provider execution.
    """
    native = binding.native
    file = binding.resource
    if native is None or file is None:
        raise ValueError("native mount receipt is absent")
    path = native.value.get("mount_path")
    if not isinstance(path, str):
        raise ValueError("native mount path is absent")
    uploaded = state.files.get(file.id)
    sandbox = state.sandbox.get(session.id, {})
    resources = state.resources.get(session.id, [])
    if (
        session.id not in state.sessions
        or uploaded is None
        or uploaded[1] != attachment.downloaded.content
        or uploaded[0].scope is None
        or uploaded[0].scope.id != session.id
        or uploaded[0].scope.type != "session"
        or (session.id, path) not in state.mount_index.get(file.id, [])
        or not any(
            item.type == "file"
            and item.id == binding.id
            and item.file_id == file.id
            and item.mount_path == path
            for item in resources
        )
        or path not in sandbox
    ):
        raise ValueError("actual SDK upload/mount materialization is unbound")
    return MountControl(
        scope=scope,
        session=session,
        binding=binding,
        attachment=attachment,
        mount_path=path,
        workspace_bytes=CapturedBytes.capture(sandbox[path]),
    )


class AttachmentCell(SurfaceModel):
    scenario_id: AttachmentScenario
    backend: Literal["anthropic", "openai", "gemini"]
    status: Literal["PENDING"] = "PENDING"
    reason: Literal["NATIVE_FETCH_MOUNT_UNBOUND", "WORKSPACE_READBACK_UNBOUND", "INGRESS_UNBOUND"]
    explanation: str = Field(min_length=1)


def attachment_cells(scenario: ScenarioFixture) -> tuple[AttachmentCell, ...]:
    """No completed cell credit without a catalog-bound native fetch/exec recording."""
    scenario_id = scenario.scenario_id
    if scenario_id not in ("QA-NEW1-PDF-BARE-MENTION-NO-DROP", "QA-D1-CANARY-TWO-TURN"):
        raise ValueError("not an attachment ingestion scenario")
    reason = (
        "WORKSPACE_READBACK_UNBOUND"
        if scenario_id == "QA-D1-CANARY-TWO-TURN"
        else "NATIVE_FETCH_MOUNT_UNBOUND"
    )
    return tuple(
        AttachmentCell(
            scenario_id=scenario_id,
            backend=backend,
            reason=reason if backend == "anthropic" else "INGRESS_UNBOUND",
            explanation=(
                "C7 PDF URL handoff lacks a catalog-bound native fetch/mount and actual "
                "workspace execution/readback; FILE hex and offline mount controls "
                "are insufficient."
                if backend == "anthropic"
                else "C7 alternate native runtime ingress is outside the signed scope."
            ),
        )
        for backend in ("anthropic", "openai", "gemini")
    )
