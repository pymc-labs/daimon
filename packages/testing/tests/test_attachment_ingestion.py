"""Frozen PDF bytes through discord.py and actual native SDK mount controls.

These controls do not award catalog completion or native workspace execution.
"""

from __future__ import annotations

import json
import shutil
from collections.abc import AsyncIterator
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import discord
import pytest
from daimon.testing.attachment_ingestion import (
    AttachmentBytes,
    AttachmentCell,
    MountControl,
    attachment_cells,
    capture_mount_control,
    read_attachment,
)
from daimon.testing.discord_surface import DiscordSurfaceHarness, OfflineDiscordGateway
from daimon.testing.ma_models import ma_agent
from daimon.testing.ma_sessions import FakeSessionsState, make_fake_sessions_handler
from daimon.testing.output_ownership import FilePin, check_pdf
from daimon.testing.provider_fixtures import ScenarioFixture, load_provider_fixtures
from discord.http import HTTPClient
from mux.contracts.ids import ResourceRef, Revision, Scope
from mux.contracts.resources import ResourceBinding, SessionSpec
from mux.drivers.anthropic.resources._authorization import ResourceAuthorization
from mux.drivers.anthropic.resources.artifacts import AnthropicArtifacts
from mux.drivers.anthropic.resources.sessions_admin import AnthropicSessionAdmin
from mux.drivers.anthropic.sessions_lifecycle import AnthropicSessions
from mux.testing.drivers.anthropic.ma import FakeMAState, MARouter
from mux.testing.drivers.anthropic.ma_transport import ScriptedTransport
from pydantic import ValidationError

if TYPE_CHECKING:
    from discord.types.message import Message as MessagePayload

DATA = Path(__file__).parents[1] / "fixtures/target53"
CATALOG = DATA / "catalog"
PACK = load_provider_fixtures(DATA / "index.json", catalog_root=CATALOG)
SCENARIOS = ("QA-NEW1-PDF-BARE-MENTION-NO-DROP", "QA-D1-CANARY-TWO-TURN")
SCOPE = Scope(tenant_id="t", account_id="a", principal_id="p", authorization_id="owned")


def scenario(name: str) -> ScenarioFixture:
    return next(row for row in PACK.scenarios if row.scenario_id == name)


class AttachmentCDN(OfflineDiscordGateway):
    """Only the original URL resolves; no network, cache or filename fallback."""

    def __init__(self, *, url: str, body: bytes) -> None:
        super().__init__(evidence_id="attachment-byte-control", channel_id=102, clock=lambda: 0.0)
        self.url, self.body = url, body
        self.reads: list[str] = []

    async def get_from_cdn(self, url: str) -> bytes:
        if url != self.url:
            raise ValueError("unowned attachment CDN URL")
        self.reads.append(url)
        return self.body

    async def close(self) -> None:
        """This offline CDN endpoint owns no network resources."""


def attachment_payload(row: ScenarioFixture, *, channel_id: int = 102) -> dict[str, Any]:
    asset = row.assets[0]
    source = FilePin(path=asset.path, sha256=asset.sha256)
    body = source.read(CATALOG)
    filename = Path(source.path).name
    url = f"https://cdn.discordapp.com/attachments/{channel_id}/105/{filename}?ex=offline"
    trigger = next(i for i in row.invocations if i.get("operation") == "host_turn")
    return {
        "id": "106",
        "channel_id": str(channel_id),
        "guild_id": "100",
        "type": 0,
        "content": "<@104> " + str(trigger["text"]),
        "attachments": [
            {
                "id": "105",
                "filename": filename,
                "size": len(body),
                "content_type": "application/pdf",
                "url": url,
                "proxy_url": url + "&proxy=1",
            }
        ],
        "embeds": [],
        "components": [],
        "author": {"id": "103", "username": "Human", "discriminator": "0000", "avatar": None},
        "mentions": [],
        "mention_roles": [],
        "mention_everyone": False,
        "pinned": False,
        "timestamp": "2026-10-11T02:00:00+00:00",
        "edited_timestamp": None,
    }


async def downloaded(name: str) -> AttachmentBytes:
    row = scenario(name)
    raw = attachment_payload(row)
    asset = row.assets[0]
    source = FilePin(path=asset.path, sha256=asset.sha256)
    harness = DiscordSurfaceHarness(
        evidence_id="byte-control", session_id="control", root_turn_id="control"
    )
    cdn = AttachmentCDN(url=raw["attachments"][0]["url"], body=source.read(CATALOG))
    state = harness.client._connection  # pyright: ignore[reportPrivateUsage]
    state.http = cast(HTTPClient, cdn)
    harness.client.http = cast(HTTPClient, cdn)
    message = discord.Message(state=state, channel=harness.thread, data=cast("MessagePayload", raw))
    try:
        result = await read_attachment(
            message,
            attachment_id=105,
            original_payload_json=json.dumps(raw),
            source=source,
            catalog_root=CATALOG,
        )
        assert cdn.reads == [cdn.url]
        return result
    finally:
        await harness.client.close()


async def chunks(body: bytes) -> AsyncIterator[bytes]:
    yield body[:17]
    yield body[17:]


def ref(kind: str, identity: str, scope: Scope = SCOPE) -> ResourceRef:
    return ResourceRef(
        id=identity,
        kind=kind,
        provider="anthropic",
        account_scope_id="owned",
        tenant_id=scope.tenant_id,
        account_id=scope.account_id,
    )


async def mount(
    transport: ScriptedTransport,
    state: FakeSessionsState,
    attachment: AttachmentBytes,
    *,
    existing_session: ResourceRef | None = None,
    scope: Scope = SCOPE,
) -> MountControl:
    async with transport.client() as client:
        agent = next(iter(state.ma.agents))
        auth = ResourceAuthorization(
            scope, frozenset({("agent", agent), ("environment", "env_control")})
        )
        if existing_session is None:
            sessions = AnthropicSessions(client, "owned", authorization=auth)
            created = await sessions.create(
                scope,
                SessionSpec(
                    agent=ref("agent", agent, scope),
                    agent_revision=Revision(local=1),
                    environment=ref("environment", "env_control", scope),
                    config_revision=1,
                ),
                key="control",
            )
            session = created.ref
        else:
            session = existing_session
        artifacts = AnthropicArtifacts(client, "owned", authorization=auth)
        uploaded = await artifacts.upload(
            scope,
            chunks(attachment.downloaded.content),
            filename=attachment.filename,
            media_type="application/pdf",
            key="control-file",
        )
        admin = AnthropicSessionAdmin(
            client,
            "owned",
            authorization=ResourceAuthorization(
                scope, frozenset({("session", session.id), ("file", uploaded.ref.id)})
            ),
        )
        binding = await admin.add_file(
            scope,
            session,
            ResourceBinding(
                id="request",
                kind="artifact",
                resource=uploaded.ref,
                target_path=attachment.filename,
            ),
            key="control-mount",
        )
        actual = await admin.list(scope, session)
        assert any(row.id == binding.id for row in actual)
        return capture_mount_control(
            attachment, scope=scope, session=session, binding=binding, state=state
        )


def native() -> tuple[ScriptedTransport, FakeSessionsState]:
    agent = ma_agent()
    state = FakeSessionsState(
        ma=FakeMAState(
            agents={agent.id: agent.model_dump(mode="json")},
            agent_versions={agent.id: {1: agent.model_dump(mode="json")}},
        )
    )
    handler = make_fake_sessions_handler(state)
    router = MARouter()
    for method in ("GET", "POST", "PATCH", "DELETE"):
        router.add(method, r"/v1/(sessions|files).*", lambda request, match: handler(request))
    return ScriptedTransport(router=router), state


@pytest.mark.parametrize("name", SCENARIOS)
async def test_real_frozen_pdf_through_discord_download_and_sdk_mount(name: str) -> None:
    attachment = await downloaded(name)
    pdf = check_pdf(attachment.downloaded.content)
    assert (pdf.status, pdf.code) == (
        ("PASS", "PARSED_PDF") if shutil.which("qpdf") else ("PENDING", "PDF_PARSER_UNAVAILABLE")
    )
    transport, state = native()
    observed = await mount(transport, state, attachment)
    assert observed.workspace_bytes.content == attachment.source.read(CATALOG)
    assert observed.mount_path.endswith("/" + attachment.filename)
    assert not observed.scenario_cell_credit
    assert MountControl.model_validate_json(observed.model_dump_json()) == observed
    upload = next(r for r in transport.requests if r.path == "/v1/files" and r.method == "POST")
    assert attachment.downloaded.content in upload.body
    assert not transport.violations


@pytest.mark.parametrize("name", SCENARIOS)
async def test_missing_pdf_parser_preserves_pending_and_real_mount_controls(
    name: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    await test_real_frozen_pdf_through_discord_download_and_sdk_mount(name)


@pytest.mark.parametrize("mutation", ["foreign_bytes", "foreign_url", "source_changed_during_read"])
async def test_real_sdk_read_refuses_foreign_download(
    mutation: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = scenario(SCENARIOS[0])
    raw = attachment_payload(row)
    asset = row.assets[0]
    source = FilePin(path=asset.path, sha256=asset.sha256)
    body = source.read(CATALOG)
    path = tmp_path / source.path
    path.parent.mkdir(parents=True)
    path.write_bytes(body)
    harness = DiscordSurfaceHarness(
        evidence_id="negative", session_id="control", root_turn_id="control"
    )
    cdn = AttachmentCDN(url=raw["attachments"][0]["url"], body=body)
    if mutation == "foreign_bytes":
        cdn.body = b"FILE=a1b2c3d4e5f6"
    elif mutation == "foreign_url":
        cdn.url += "&foreign=1"
    else:

        async def changed_source(url: str) -> bytes:
            path.write_bytes(b"changed during download")
            return body

        monkeypatch.setattr(cdn, "get_from_cdn", changed_source)
    state = harness.client._connection  # pyright: ignore[reportPrivateUsage]
    state.http = cast(HTTPClient, cdn)
    harness.client.http = cast(HTTPClient, cdn)
    message = discord.Message(state=state, channel=harness.thread, data=cast("MessagePayload", raw))
    try:
        with pytest.raises(ValueError):
            await read_attachment(
                message,
                attachment_id=105,
                original_payload_json=json.dumps(raw),
                source=source,
                catalog_root=tmp_path,
            )
    finally:
        await harness.client.close()


@pytest.mark.parametrize(
    "mutation", ["scope", "file", "mount_path", "cell_credit", "workspace_bytes"]
)
async def test_mount_json_roundtrip_cannot_change_ownership_or_claim_cell_credit(
    mutation: str,
) -> None:
    attachment = await downloaded(SCENARIOS[0])
    transport, state = native()
    proof = await mount(transport, state, attachment)
    raw = json.loads(proof.model_dump_json())
    if mutation == "scope":
        raw["scope"]["account_id"] = "foreign"
    elif mutation == "file":
        raw["binding"]["resource"]["id"] = "foreign"
    elif mutation == "mount_path":
        raw["mount_path"] = "/foreign.pdf"
    elif mutation == "cell_credit":
        raw["scenario_cell_credit"] = True
    else:
        raw["workspace_bytes"]["sha256"] = "0" * 64
    with pytest.raises(ValidationError):
        MountControl.model_validate_json(json.dumps(raw))


@pytest.mark.parametrize(
    "mutation",
    [
        "sandbox_removed",
        "sandbox_changed",
        "upload_changed",
        "mount_index",
        "resource_removed",
        "foreign_session",
    ],
)
async def test_mount_control_rejects_missing_or_foreign_byte_join(mutation: str) -> None:
    attachment = await downloaded(SCENARIOS[0])
    transport, state = native()
    proof = await mount(transport, state, attachment)
    assert proof.binding.resource is not None
    file_id = proof.binding.resource.id
    session = proof.session
    if mutation == "sandbox_removed":
        state.sandbox[session.id].clear()
    elif mutation == "sandbox_changed":
        state.sandbox[session.id][proof.mount_path] = b"fixture-declared PDF"
    elif mutation == "upload_changed":
        state.files[file_id] = (state.files[file_id][0], b"foreign bytes")
    elif mutation == "mount_index":
        state.mount_index[file_id] = []
    elif mutation == "resource_removed":
        state.resources[session.id] = []
    else:
        session = session.model_copy(update={"id": "foreign-session"})
    with pytest.raises(ValueError):
        capture_mount_control(
            attachment, scope=SCOPE, session=session, binding=proof.binding, state=state
        )


@pytest.mark.parametrize(
    "mutation", ["message_id", "channel_id", "attachment_id", "url", "sha256", "content"]
)
async def test_attachment_json_roundtrip_rejects_identity_or_byte_changes(mutation: str) -> None:
    original = await downloaded(SCENARIOS[0])
    raw = json.loads(original.model_dump_json())
    if mutation in {"message_id", "channel_id", "attachment_id"}:
        raw[mutation] = "999"
    elif mutation == "url":
        raw["url"] += "&foreign=1"
    elif mutation == "sha256":
        raw["source"]["sha256"] = "0" * 64
    else:
        raw["downloaded"]["content"] = "RklMRT1hMWIyYzNkNGU1ZjY="
    with pytest.raises(ValidationError):
        AttachmentBytes.model_validate_json(json.dumps(raw))


@pytest.mark.parametrize("name", SCENARIOS)
def test_all_three_catalog_cells_pending_even_when_replay_has_file_hex(name: str) -> None:
    cells = attachment_cells(scenario(name))
    assert len(cells) == 3 and {c.backend for c in cells} == {"anthropic", "openai", "gemini"}
    for cell in cells:
        assert cell.status == "PENDING"
        assert AttachmentCell.model_validate_json(cell.model_dump_json()) == cell
        raw = json.loads(cell.model_dump_json())
        raw["status"] = "PASS"
        with pytest.raises(ValidationError):
            AttachmentCell.model_validate_json(json.dumps(raw))
    if name == SCENARIOS[1]:
        assert "FILE=a1b2c3d4e5f6" in (DATA / "anthropic" / (name + ".json")).read_text()
        assert cells[0].reason == "WORKSPACE_READBACK_UNBOUND"


def test_unrelated_scenario_is_not_silently_bound() -> None:
    with pytest.raises(ValueError):
        attachment_cells(scenario("QA-D12-PDF-CLAIM-HAS-PDF"))
