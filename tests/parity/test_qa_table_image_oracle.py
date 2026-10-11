"""Actual renderer pixels; adversarial receipt/window controls never earn PASS."""

from __future__ import annotations

import hashlib
import io
import json
from dataclasses import replace
from typing import Literal

import pytest
from daimon.adapters.discord.tables import render_discord_tables
from daimon.core import tables as core_tables
from daimon.testing.table_image_oracle import (
    SOURCE_SHA256,
    TableImageIdentity,
    TableImageTarget,
    UploadedTableImage,
    check_table_image,
)
from PIL import Image, ImageDraw
from pydantic import ValidationError

from .test_qa_new37_table import answers

type Backend = Literal["anthropic", "openai", "gemini"]


@pytest.fixture
async def pixels() -> bytes:
    _, files = await render_discord_tables(answers()[0], enabled=True)
    assert len(files) == 1
    return files[0].fp.read()


def binding(backend: Backend = "anthropic") -> TableImageTarget:
    return TableImageTarget(
        identity=TableImageIdentity(
            scenario_sha256=SOURCE_SHA256,
            backend=backend,
            run_id="run-1",
            session_id="session-1",
            root_turn_id="root-1",
            channel_id="channel-1",
            message_id="answer-1",
            attachment_url="offline://attachment/upload-1/table-1.png",
            native_evidence_id="native-1",
            snapshot_evidence_id="snapshot-1",
            final_wait_id="wait-1",
            trigger_location="steps[1]",
            final_wait_location="steps[2]",
        ),
        native_text=answers()[0],
        native_authority="record",
        native_sequence=10,
        final_wait_sequence=30,
        snapshot_sequence=31,
        started_s=10.0,
        native_s=11.0,
        final_wait_s=13.0,
        snapshot_s=14.0,
        render_tables=True,
        surviving_answer_bound=True,
        snapshot_complete=True,
        attachment_urls=("offline://attachment/upload-1/table-1.png",),
    )


def receipt(target: TableImageTarget, body: bytes) -> UploadedTableImage:
    return UploadedTableImage(
        identity=target.identity,
        upload_evidence_id="upload-1",
        upload_sequence=20,
        upload_s=12.0,
        snapshot_sequence=target.snapshot_sequence,
        snapshot_s=target.snapshot_s,
        sha256=hashlib.sha256(body).hexdigest(),
        byte_length=len(body),
        delivered=True,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ("anthropic", "openai", "gemini"))
async def test_all_matched_fixture_inputs_prove_only_the_bound_image_domain(
    backend: Backend,
) -> None:
    # Actual frozen native answers are equivalent across the three providers.
    text = answers()[("anthropic", "openai", "gemini").index(backend)]
    visible, files = await render_discord_tables(text, enabled=True)
    assert visible == "[Table attached: table-1.png]\n" and len(files) == 1
    body = files[0].fp.read()
    target = binding(backend)
    upload = receipt(target, body)
    result = check_table_image(target, upload, png=body)
    assert result.status == "PASS" and result.kind == "table_image"
    assert result.code == "BOUND_TABLE_PIXELS_MATCH"
    # JSON roundtrip retains every identity and sequence boundary.
    assert (
        check_table_image(
            TableImageTarget.model_validate_json(target.model_dump_json()),
            UploadedTableImage.model_validate_json(upload.model_dump_json()),
            png=body,
        )
        == result
    )


def test_recompression_preserves_pixel_outcome_without_byte_parity(pixels: bytes) -> None:
    target = binding()
    with Image.open(io.BytesIO(pixels)) as image:
        alternate = io.BytesIO()
        image.save(alternate, format="PNG", compress_level=0)
    body = alternate.getvalue()
    assert body != pixels
    assert check_table_image(target, receipt(target, body), png=body).status == "PASS"


@pytest.mark.asyncio
async def test_actual_native_cell_values_are_input_rather_than_fixed_provider_output(
    pixels: bytes,
) -> None:
    text = answers()[0].replace("Apple", "Mango").replace("$1", "$11")
    _, files = await render_discord_tables(text, enabled=True)
    body = files[0].fp.read()
    target = binding().model_copy(update={"native_text": text})
    assert body != pixels
    assert (
        check_table_image(target, receipt(target, body), png=body).code
        == "BOUND_TABLE_PIXELS_MATCH"
    )
    # Swapping those new pixels onto the old authoritative native input is a gap.
    old = binding()
    assert check_table_image(old, receipt(old, body), png=body).status == "PENDING"


@pytest.mark.asyncio
async def test_production_parser_mutation_cannot_change_the_oracle_reference(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    text = answers()[0]
    parsed = core_tables.parse_tables(text)
    assert len(parsed) == 1 and isinstance(parsed[0], core_tables.MarkdownTable)
    changed = replace(parsed[0], rows=tuple((*row[:2], "$0") for row in parsed[0].rows))

    def changed_parser(_text: str) -> list[str | core_tables.MarkdownTable]:
        return [changed]

    monkeypatch.setattr(core_tables, "parse_tables", changed_parser)
    _, files = await render_discord_tables(text, enabled=True)
    body = files[0].fp.read()
    target = binding()
    assert check_table_image(target, receipt(target, body), png=body).status == "PENDING"


@pytest.mark.parametrize(
    "field",
    (
        "backend",
        "run_id",
        "session_id",
        "root_turn_id",
        "channel_id",
        "message_id",
        "attachment_url",
        "native_evidence_id",
        "snapshot_evidence_id",
        "final_wait_id",
        "scenario_sha256",
    ),
)
def test_foreign_identity_never_passes_after_json_roundtrip(pixels: bytes, field: str) -> None:
    target = binding()
    raw = receipt(target, pixels).model_dump(mode="json")
    identity = raw["identity"]
    assert isinstance(identity, dict)
    identity[field] = (
        "gemini"
        if field == "backend"
        else "0" * 64
        if field == "scenario_sha256"
        else "offline://attachment/foreign/table-1.png"
        if field == "attachment_url"
        else "foreign"
    )
    upload = UploadedTableImage.model_validate_json(json.dumps(raw))
    assert check_table_image(target, upload, png=pixels).status == "PENDING"


@pytest.mark.parametrize(
    "fault",
    (
        "draft",
        "incomplete",
        "deleted",
        "default_off",
        "final_wait_equal",
        "pre_final_snapshot",
        "extra_attachment",
        "no_attachment",
        "wrong_source",
    ),
)
def test_unbound_target_never_passes(pixels: bytes, fault: str) -> None:
    target = binding()
    upload = receipt(target, pixels)
    if fault == "draft":
        target = target.model_copy(update={"native_authority": "preview"})
    elif fault == "incomplete":
        target = target.model_copy(update={"snapshot_complete": False})
    elif fault == "deleted":
        target = target.model_copy(update={"surviving_answer_bound": False})
    elif fault == "default_off":
        target = target.model_copy(update={"render_tables": False})
    elif fault == "final_wait_equal":
        target = target.model_copy(update={"snapshot_sequence": target.final_wait_sequence})
    elif fault == "pre_final_snapshot":
        target = target.model_copy(update={"snapshot_sequence": target.final_wait_sequence - 1})
    elif fault == "extra_attachment":
        target = target.model_copy(update={"attachment_urls": (*target.attachment_urls, "unknown")})
    elif fault == "no_attachment":
        target = target.model_copy(update={"attachment_urls": ()})
    else:
        identity = target.identity.model_copy(update={"scenario_sha256": "0" * 64})
        target = target.model_copy(update={"identity": identity})
        upload = upload.model_copy(update={"identity": identity})
    target = TableImageTarget.model_validate_json(target.model_dump_json())
    assert check_table_image(target, upload, png=pixels).status == "PENDING"


@pytest.mark.parametrize(
    "fault",
    (
        "not_delivered",
        "before_native",
        "after_wait",
        "pretrigger_clock",
        "before_native_clock",
        "after_wait_clock",
        "stale_snapshot_sequence",
        "stale_snapshot_clock",
        "duplicate_evidence",
    ),
)
def test_stale_upload_or_snapshot_never_passes(pixels: bytes, fault: str) -> None:
    target = binding()
    changes = {
        "not_delivered": {"delivered": False},
        "before_native": {"upload_sequence": target.native_sequence},
        "after_wait": {"upload_sequence": target.final_wait_sequence + 1},
        "pretrigger_clock": {"upload_s": target.started_s - 1},
        "before_native_clock": {"upload_s": target.native_s - 0.5},
        "after_wait_clock": {"upload_s": target.final_wait_s + 1},
        "stale_snapshot_sequence": {"snapshot_sequence": target.final_wait_sequence},
        "stale_snapshot_clock": {"snapshot_s": target.final_wait_s},
        "duplicate_evidence": {"upload_evidence_id": target.identity.native_evidence_id},
    }
    upload = receipt(target, pixels).model_copy(update=changes[fault])
    upload = UploadedTableImage.model_validate_json(upload.model_dump_json())
    assert check_table_image(target, upload, png=pixels).status == "PENDING"


@pytest.mark.parametrize(
    "fault", ("blank", "missing_price", "cropped", "wrong_cell", "hidden_glyphs")
)
def test_actual_mutated_png_cannot_claim_readable_table(pixels: bytes, fault: str) -> None:
    target = binding()
    with Image.open(io.BytesIO(pixels)) as source:
        image = source.copy()
    draw = ImageDraw.Draw(image)
    if fault == "blank":
        draw.rectangle((0, 0, 900, 245), fill="white")
    elif fault == "missing_price":
        draw.rectangle((600, 0, 900, 245), fill="white")
    elif fault == "cropped":
        image = image.crop((0, 0, 600, 245))
    elif fault == "wrong_cell":
        draw.rectangle((0, 49, 300, 98), fill="white")
        draw.text((12, 61), "Mango", fill="black")
    else:
        draw.rectangle((0, 0, 900, 49), fill="#0C1F40")
    encoded = io.BytesIO()
    image.save(encoded, format="PNG")
    body = encoded.getvalue()
    # Even a genuine newly uploaded receipt for the bad pixels cannot PASS.
    assert check_table_image(target, receipt(target, body), png=body).status == "PENDING"


def test_corrupt_or_substituted_bytes_fail_and_missing_bytes_stay_pending(pixels: bytes) -> None:
    target = binding()
    upload = receipt(target, pixels)
    assert (
        check_table_image(target, upload, png=pixels + b"substituted").code
        == "TABLE_UPLOAD_BYTES_CHANGED"
    )
    assert (
        check_table_image(target, receipt(target, b"not png"), png=b"not png").code
        == "TABLE_PNG_UNREADABLE"
    )
    assert check_table_image(target, upload, png=None).status == "PENDING"
    assert check_table_image(target, None, png=pixels).status == "PENDING"


def test_wrong_wait_location_filename_only_and_untyped_flags_are_rejected() -> None:
    target = binding()
    raw = target.model_dump(mode="json")
    identity = raw["identity"]
    assert isinstance(identity, dict)
    identity["final_wait_location"] = "steps[1]"
    with pytest.raises(ValidationError):
        TableImageTarget.model_validate_json(json.dumps(raw))
    with pytest.raises(ValidationError):
        TableImageIdentity.model_validate(
            {**target.identity.model_dump(), "attachment_url": "table-1.png"}
        )
    with pytest.raises(ValidationError):
        TableImageTarget.model_validate({**target.model_dump(), "render_tables": "true"})
