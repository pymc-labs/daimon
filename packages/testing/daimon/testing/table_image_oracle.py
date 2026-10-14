"""Narrow unscored NEW37 proof over uploaded PNG pixels and owned lineage.

This does not complete generic text capture or certify a catalog cell. The
caller supplies the independently selected surviving answer and final-wait
snapshot from its owned capture, then downloads that exact attachment. Only
the reviewed single-line Discord Inter table layout is supported; other
layouts remain PENDING. PNG encoding and provider IDs are not parity anchors.
"""

from __future__ import annotations

import hashlib
import io
import re
from typing import Literal

from daimon.core.fonts import FONT_DIR
from daimon.core.tables import Alignment, MarkdownTable
from daimon.testing.outcome_oracle import CheckResult
from PIL import Image, ImageChops, ImageDraw, ImageFont, UnidentifiedImageError
from pydantic import BaseModel, ConfigDict, Field, model_validator

NEW37 = "QA-NEW37-MARKDOWN-TABLE-READABLE"
# Frozen scored source; the pixel proof is an additional unscored control.
SOURCE_SHA256 = "9f12f8845f03468665c1bc13a86122ff2922fd3bf59dd8d02a0387da7831a2ca"
FONT_SHA256 = {
    "Inter-Regular.ttf": "40d692fce188e4471e2b3cba937be967878f631ad3ebbbdcd587687c7ebe0c82",
    "Inter-Bold.ttf": "288316099b1e0a47a4716d159098005eef7c0066921f34e3200393dbdb01947f",
}


class ImageModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True, allow_inf_nan=False)


class TableImageIdentity(ImageModel):
    """Expected identity comes from routing/native capture, never the PNG name."""

    scenario_id: Literal["QA-NEW37-MARKDOWN-TABLE-READABLE"] = NEW37
    scenario_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    backend: Literal["anthropic", "openai", "gemini"]
    run_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    root_turn_id: str = Field(min_length=1)
    channel_id: str = Field(min_length=1)
    message_id: str = Field(min_length=1)
    # Distinct upload URL/ID, not filename-based artifact lookup.
    attachment_url: str = Field(
        pattern=r"^(?:offline://attachment/[^/]+|https://cdn\.discordapp\.com/attachments/[0-9]+/[0-9]+)/table-1\.png(?:\?[^#]*)?$"
    )
    filename: Literal["table-1.png"] = "table-1.png"
    native_evidence_id: str = Field(min_length=1)
    snapshot_evidence_id: str = Field(min_length=1)
    final_wait_id: str = Field(min_length=1)
    trigger_location: Literal["steps[1]"]
    final_wait_location: Literal["steps[2]"]


class TableImageTarget(ImageModel):
    """Independent runner selection after the final wait; no inferred defaults."""

    identity: TableImageIdentity
    native_text: str = Field(min_length=1, max_length=10000)
    native_authority: Literal["record", "reconciled", "preview", "gap"]
    native_sequence: int = Field(ge=0)
    final_wait_sequence: int = Field(ge=0)
    snapshot_sequence: int = Field(ge=0)
    started_s: float = Field(ge=0)
    native_s: float = Field(ge=0)
    final_wait_s: float = Field(ge=0)
    snapshot_s: float = Field(ge=0)
    render_tables: bool = False
    # Complete post history and an independently reconstructed survivor.
    surviving_answer_bound: bool = False
    snapshot_complete: bool = False
    # Every surviving image must be accounted for before any domain promotion.
    attachment_urls: tuple[str, ...] = ()

    @model_validator(mode="after")
    def ordered(self) -> TableImageTarget:
        if not self.started_s <= self.native_s <= self.final_wait_s <= self.snapshot_s:
            raise ValueError("image snapshot clock is outside the final wait")
        return self


class UploadedTableImage(ImageModel):
    """Receipt tied to the actual upload and assertion-time surviving snapshot.

    N5 owns byte retrieval; N9 joins its receipt with the selected native root,
    post event and final-wait snapshot. A draft/render buffer is insufficient.
    """

    identity: TableImageIdentity
    upload_evidence_id: str = Field(min_length=1)
    upload_sequence: int = Field(ge=0)
    upload_s: float = Field(ge=0)
    snapshot_sequence: int = Field(ge=0)
    snapshot_s: float = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    byte_length: int = Field(gt=0, le=2_000_000)
    delivered: bool = False


def _result(status: Literal["PASS", "FAIL", "PENDING"], code: str, *ids: str) -> CheckResult:
    return CheckResult(kind="table_image", turn=1, status=status, code=code, evidence_ids=ids)


def _plain_table(text: str) -> MarkdownTable | None:
    """Read the narrow native input independently of the production parser."""
    lines = text.strip().splitlines()
    if len(lines) != 6 or "\\" in text or "`" in text:
        return None
    cells: list[tuple[str, ...]] = []
    for line in lines:
        value = line.strip()
        if not value.startswith("|") or not value.endswith("|"):
            return None
        row = tuple(part.strip() for part in value[1:-1].split("|"))
        if len(row) != 3:
            return None
        cells.append(row)
    separators = cells[1]
    if not all(re.fullmatch(r":?-{3,}:?", cell) for cell in separators):
        return None
    data = tuple(cells[2:])
    alignments: list[Alignment] = []
    for column, separator in enumerate(separators):
        if separator.startswith(":") and separator.endswith(":"):
            alignments.append("center")
        elif separator.endswith(":") or all(
            re.fullmatch(r"[-+]?[$]?[\d,.]+%?", row[column]) for row in data
        ):
            alignments.append("right")
        else:
            alignments.append("left")
    return MarkdownTable(text, cells[0], data, tuple(alignments))


def _template(table: MarkdownTable) -> Image.Image | None:
    """Independent glyph projection for the narrow reviewed layout.

    It never calls table_png or render_discord_tables and never uses the image
    under test to generate its reference. Every expected cell must contain
    visible, unclipped glyphs at the known 18px Inter layout. Layout changes
    need review rather than silently granting readability from a hash.
    """
    if table.headers != ("Fruit", "Colour", "Price") or len(table.rows) != 4:
        return None
    try:
        for name, digest in FONT_SHA256.items():
            if hashlib.sha256((FONT_DIR / name).read_bytes()).hexdigest() != digest:
                return None
    except OSError:
        return None
    regular = ImageFont.truetype(str(FONT_DIR / "Inter-Regular.ttf"), 18)
    bold = ImageFont.truetype(str(FONT_DIR / "Inter-Bold.ttf"), 18)
    reference = Image.new("RGB", (900, 245), "white")
    canvas = ImageDraw.Draw(reference)
    for row_index, row in enumerate((table.headers, *table.rows)):
        font = bold if row_index == 0 else regular
        y = row_index * 49
        canvas.rectangle(
            (0, y, 900, y + 49),
            fill="#0C1F40" if row_index == 0 else ("#F7F7F7" if row_index % 2 else "#FFFFFF"),
        )
        for column, cell in enumerate(row):
            if not cell or any(not 32 <= ord(char) <= 126 for char in cell):
                return None
            width = font.getlength(cell)
            if width > 276:
                return None  # Wrapped/multiline cells are outside this proof.
            x = column * 300 + 12
            if table.alignments[column] == "right":
                x = (column + 1) * 300 - 12 - width
            elif table.alignments[column] == "center":
                x = column * 300 + (300 - width) / 2
            canvas.text(
                (x, y + 12), cell, font=font, fill="#FFFFFF" if row_index == 0 else "#0C1F40"
            )
        canvas.line((0, y + 48, 900, y + 48), fill="#B4E7DD", width=1)
    return reference


def check_table_image(
    target: TableImageTarget, upload: UploadedTableImage | None, *, png: bytes | None
) -> CheckResult:
    """Verify a complete single-image domain; never promote generic text coverage."""
    if not target.render_tables:
        return _result("PENDING", "TABLE_RENDERING_NOT_SELECTED")
    if target.identity.scenario_sha256 != SOURCE_SHA256:
        return _result("PENDING", "TABLE_SOURCE_UNBOUND")
    if target.native_authority not in ("record", "reconciled"):
        return _result("PENDING", "TABLE_NATIVE_RECORD_MISSING")
    if not target.surviving_answer_bound or not target.snapshot_complete:
        return _result("PENDING", "TABLE_SURVIVOR_UNBOUND")
    if not target.native_sequence < target.final_wait_sequence < target.snapshot_sequence:
        return _result("PENDING", "TABLE_FINAL_WAIT_UNBOUND")
    if target.attachment_urls != (target.identity.attachment_url,):
        return _result("PENDING", "TABLE_IMAGE_DOMAIN_INCOMPLETE")
    if upload is None or png is None or not upload.delivered:
        return _result("PENDING", "TABLE_UPLOAD_MISSING")
    if upload.identity != target.identity:
        return _result("PENDING", "TABLE_UPLOAD_IDENTITY_UNBOUND")
    if (
        upload.snapshot_sequence != target.snapshot_sequence
        or upload.snapshot_s != target.snapshot_s
        or not target.native_sequence < upload.upload_sequence <= target.final_wait_sequence
        or not target.native_s <= upload.upload_s <= target.final_wait_s
    ):
        return _result("PENDING", "TABLE_UPLOAD_WINDOW_UNBOUND")
    ids = (
        target.identity.native_evidence_id,
        upload.upload_evidence_id,
        target.identity.snapshot_evidence_id,
    )
    if len(set(ids)) != 3 or target.identity.final_wait_id in ids:
        return _result("PENDING", "TABLE_EVIDENCE_IDENTITY_UNBOUND")
    if len(png) != upload.byte_length or hashlib.sha256(png).hexdigest() != upload.sha256:
        return _result("FAIL", "TABLE_UPLOAD_BYTES_CHANGED", *ids)
    try:
        table = _plain_table(target.native_text)
        if table is None:
            return _result("PENDING", "TABLE_LAYOUT_UNSUPPORTED", *ids)
        reference = _template(table)
        if reference is None:
            return _result("PENDING", "TABLE_LAYOUT_UNSUPPORTED", *ids)
        with Image.open(io.BytesIO(png)) as decoded:
            if decoded.format != "PNG" or decoded.size != reference.size or decoded.mode != "RGB":
                return _result("PENDING", "TABLE_PIXEL_LAYOUT_UNSUPPORTED", *ids)
            decoded.load()
            # Compare decoded pixels; recompression/metadata never change parity.
            if ImageChops.difference(decoded, reference).getbbox() is not None:
                return _result("PENDING", "TABLE_PIXELS_UNVERIFIED", *ids)
    except (OSError, ValueError, UnidentifiedImageError, Image.DecompressionBombError):
        return _result("FAIL", "TABLE_PNG_UNREADABLE", *ids)
    return _result("PASS", "BOUND_TABLE_PIXELS_MATCH", *ids)
