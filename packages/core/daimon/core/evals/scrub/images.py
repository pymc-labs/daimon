"""Images: OCR the words, mask the ones that hold something private, flag for a person.

Without the `tesseract` binary the whole image is blanked: nothing can be checked, so nothing is
kept. Faces are never detected here, so every image is returned `needs_human_check`.
"""

from __future__ import annotations

import csv
import io
import shutil
import subprocess
from dataclasses import dataclass
from typing import Final, Protocol

from PIL import Image, ImageDraw

_FILL: Final = (64, 64, 64)
_MIN_CONF: Final = 30.0
_PAD: Final = 2


class Spans(Protocol):
    def spans(self, text: str) -> list[tuple[int, int]]: ...


@dataclass(frozen=True)
class Word:
    text: str
    box: tuple[int, int, int, int]
    line: tuple[int, int, int]


def ocr_available() -> bool:
    return shutil.which("tesseract") is not None


def _words(image: Image.Image) -> list[Word]:
    png = io.BytesIO()
    image.convert("RGB").save(png, format="PNG")
    done = subprocess.run(
        ["tesseract", "stdin", "stdout", "tsv"],
        input=png.getvalue(), capture_output=True, check=True, timeout=120,
    )  # fmt: skip
    rows = csv.DictReader(io.StringIO(done.stdout.decode()), delimiter="\t", quoting=csv.QUOTE_NONE)
    words: list[Word] = []
    for row in rows:
        text = (row.get("text") or "").strip()
        if text and float(row.get("conf") or -1) >= _MIN_CONF:
            left, top = int(row["left"]), int(row["top"])
            words.append(
                Word(
                    text,
                    (left, top, left + int(row["width"]), top + int(row["height"])),
                    (int(row["block_num"]), int(row["par_num"]), int(row["line_num"])),
                )
            )
    return words


def _hidden(words: list[Word], spans: Spans) -> list[Word]:
    """Words that overlap a span in their line's text."""
    hidden: list[Word] = []
    for line in dict.fromkeys(w.line for w in words):
        in_line = [w for w in words if w.line == line]
        text = ""
        offsets: list[tuple[int, int]] = []
        for w in in_line:
            offsets.append((len(text), len(text) + len(w.text)))
            text += w.text + " "
        found = spans.spans(text)
        hidden += [
            w
            for w, (a, b) in zip(in_line, offsets, strict=True)
            if any(a < e and s < b for s, e in found)
        ]
    return hidden


def redact_image(data: bytes, spans: Spans) -> tuple[bytes, str]:
    """The image with private words masked (all of it without OCR), and its media type."""
    with Image.open(io.BytesIO(data)) as opened:
        fmt = opened.format if opened.format in Image.SAVE else "PNG"
        image = opened.convert("RGB")
    draw = ImageDraw.Draw(image)
    if ocr_available():
        try:
            targets = [w.box for w in _hidden(_words(image), spans)]
        except (subprocess.SubprocessError, OSError, ValueError, KeyError):
            targets = [(0, 0, *image.size)]
    else:
        targets = [(0, 0, *image.size)]
    for left, top, right, bottom in targets:
        draw.rectangle((left - _PAD, top - _PAD, right + _PAD, bottom + _PAD), fill=_FILL)
    out = io.BytesIO()
    image.save(out, format=fmt)
    return out.getvalue(), Image.MIME.get(fmt or "PNG", "image/png")
