"""Rebuilding blobs with fakes: text, JSON, Office files, PDFs with a text layer and images.

`rebuild` returns the new bytes and how they were made, or None when it cannot handle the type
(the sweep then flags the blob). The same code with a counting rewriter finds leftovers.
"""

from __future__ import annotations

import io
import json
import re
import zipfile
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Final, Literal, Protocol, cast
from xml.etree import ElementTree as ET

from daimon.core.evals.scrub.images import redact_image
from PIL import Image
from pydantic import JsonValue

_TEXT_TYPES: Final = {
    "application/json", "application/x-ndjson", "application/xml", "application/yaml",
    "application/x-yaml", "application/toml", "application/javascript", "application/x-sh",
    "application/sql", "application/csv", "application/x-httpd-php",
}  # fmt: skip
_TEXT_SUFFIXES: Final = {
    ".md", ".txt", ".csv", ".tsv", ".json", ".ndjson", ".jsonl", ".yaml", ".yml", ".toml", ".py",
    ".js", ".ts", ".sh", ".sql", ".html", ".xml", ".log", ".ini", ".cfg", ".rst",
}  # fmt: skip
_OFFICE: Final = {
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": "pptx",
}
_NS_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_NS_A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
_NS_X = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_PARAGRAPHS: Final = (f"{_NS_W}p", f"{_NS_A}p", f"{_NS_X}si", f"{_NS_X}is")
_RUNS: Final = (f"{_NS_W}t", f"{_NS_W}delText", f"{_NS_A}t", f"{_NS_X}t")
_IMAGE_SUFFIXES: Final = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".tif", ".tiff"}
_XML_PARTS: Final = re.compile(r"\.(xml|rels|vml)$")
_OOXML_SUFFIXES: Final = {".docx", ".xlsx", ".pptx", ".docm", ".xlsm", ".pptm"}
_BLANK_SVG: Final = b'<svg xmlns="http://www.w3.org/2000/svg"/>'
# Who wrote or changed a file: blanked, since nothing marks them as names.
_AUTHOR_TAGS: Final = frozenset(
    {"creator", "lastModifiedBy", "Company", "Manager", "HyperlinkBase"}
)
_AUTHOR_ATTRS: Final = {"author": "Author", "initials": "A", "userId": "", "providerId": ""}
_MAX_PART: Final = 64 * 1024 * 1024
_MAX_UNPACKED: Final = 256 * 1024 * 1024
_MAX_PARTS: Final = 10_000
_PDF_LINES: Final = 58
_PDF_WIDTH: Final = 92


class Rewriter(Protocol):
    """What the file code needs from a scrubber; a probe that only counts also fits."""

    def text(self, text: str, *, names: bool = True) -> str: ...
    def learn(self, node: JsonValue, *, key: str = "") -> None: ...
    def value(self, node: JsonValue, *, key: str = "") -> JsonValue: ...
    def spans(self, text: str) -> list[tuple[int, int]]: ...


@dataclass(frozen=True)
class Rebuilt:
    data: bytes
    media_type: str
    scrub: Literal["rebuilt", "redacted"]
    needs_human_check: bool


def _kind(media_type: str, name: str | None) -> str | None:
    suffix = PurePosixPath(name or "").suffix.lower()
    if media_type in _OFFICE:
        return _OFFICE[media_type]
    if suffix in (".docx", ".xlsx", ".pptx") and media_type == "application/octet-stream":
        return suffix[1:]
    if media_type == "application/pdf" or suffix == ".pdf":
        return "pdf"
    if media_type.startswith("image/"):
        return "image"
    if media_type.startswith("text/") or media_type in _TEXT_TYPES or suffix in _TEXT_SUFFIXES:
        return "ndjson" if "ndjson" in media_type or suffix in (".ndjson", ".jsonl") else (
            "json" if media_type == "application/json" or suffix == ".json" else "text"
        )  # fmt: skip
    return None


def rebuild(data: bytes, media_type: str, name: str | None, rw: Rewriter) -> Rebuilt | None:
    kind = _kind(media_type, name)
    try:
        if kind in ("text", "json", "ndjson"):
            return _text(data, media_type, kind, rw)
        if kind in ("docx", "xlsx", "pptx"):
            return _office(data, media_type, rw)
        if kind == "pdf":
            return _pdf(data, rw)
        if kind == "image":
            redacted, out_type = redact_image(data, rw)
            return Rebuilt(redacted, out_type, "redacted", True)
    except (
        UnicodeDecodeError, ValueError, zipfile.BadZipFile, ET.ParseError, OSError, RuntimeError,
        ImportError, Image.DecompressionBombError,
    ):  # fmt: skip
        return None  # the sweep flags it; an optional library missing reads the same
    return None


def _text(data: bytes, media_type: str, kind: str, rw: Rewriter) -> Rebuilt:
    text = data.decode("utf-8")
    if kind == "json":
        try:
            return Rebuilt(_dump(rw.value(json.loads(text))), media_type, "rebuilt", False)
        except json.JSONDecodeError:
            pass
    if kind == "ndjson":
        lines = [rw.value(json.loads(line)) for line in text.splitlines() if line.strip()]
        return Rebuilt(
            b"".join(_dump(v, indent=None) + b"\n" for v in lines), media_type, "rebuilt", False
        )
    return Rebuilt(rw.text(text).encode(), media_type, "rebuilt", False)


def _dump(value: JsonValue, indent: int | None = 2) -> bytes:
    return json.dumps(value, ensure_ascii=False, indent=indent).encode()


# ---------- Office ----------


def _rewrite_xml(data: bytes, rw: Rewriter) -> bytes:
    if b"<!DOCTYPE" in data[:4096]:
        raise ValueError("DTD in XML")
    for _, ns in ET.iterparse(io.BytesIO(data), events=("start-ns",)):
        prefix, uri = cast("tuple[str, str]", ns)  # start-ns events carry (prefix, uri)
        ET.register_namespace(prefix, uri)
    root = ET.fromstring(data)
    inside: set[int] = set()
    paragraphs: list[tuple[list[ET.Element], str]] = []
    for para in reversed(list(root.iter())):
        if para.tag not in _PARAGRAPHS:
            continue
        runs = [r for r in para.iter() if r.tag in _RUNS and id(r) not in inside]
        inside.update(id(r) for r in runs)
        paragraphs.append((runs, "".join(r.text or "" for r in runs)))
    rw.learn([joined for _, joined in paragraphs])  # a name found once is replaced everywhere
    for runs, joined in paragraphs:
        scrubbed = rw.text(joined)
        if runs and scrubbed != joined:
            runs[0].text = scrubbed
            runs[0].set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
            for run in runs[1:]:
                run.text = ""
    for el in root.iter():
        local = el.tag.rsplit("}", 1)[-1]
        if local in _AUTHOR_TAGS:
            el.text = ""
        elif id(el) not in inside and el.text and el.text.strip():
            el.text = rw.text(el.text)
        for key, val in el.attrib.items():
            name = key.rsplit("}", 1)[-1]
            el.set(key, _AUTHOR_ATTRS[name] if name in _AUTHOR_ATTRS else rw.text(val, names=False))
    head = re.match(rb"(<\?xml[^>]*\?>\s*)?", data)
    prolog = head.group(0) if head else b""
    body = ET.tostring(root, encoding="unicode")
    original = re.search(r"<[^?!/][^>]*>", data[len(prolog) :].decode("utf-8"))
    rebuilt = re.match(r"<[^>]*>", body)
    if original and rebuilt and not rebuilt.group().endswith("/>"):
        body = original.group() + body[rebuilt.end() :]  # keep every declared namespace
    return prolog + body.encode()


def _office(data: bytes, media_type: str, rw: Rewriter) -> Rebuilt:
    """Every part rewritten: XML with fakes, images redacted, embedded Office files rebuilt the
    same way, and every other binary (OLE objects, EMF, SVG, fonts) blanked: none is kept."""
    out = io.BytesIO()
    flagged = False
    with (
        zipfile.ZipFile(io.BytesIO(data)) as src,
        zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as dst,
    ):
        infos = src.infolist()
        if len(infos) > _MAX_PARTS or sum(i.file_size for i in infos) > _MAX_UNPACKED:
            raise ValueError("Office file too large to rebuild")
        for info in infos:
            if info.file_size > _MAX_PART:
                raise ValueError("Office part too large to rebuild")
            part = src.read(info)
            suffix = PurePosixPath(info.filename).suffix.lower()
            if _XML_PARTS.search(info.filename) or info.filename == "[Content_Types].xml":
                part = _rewrite_xml(part, rw)
            elif suffix in _IMAGE_SUFFIXES:
                part = redact_image(part, rw)[0]  # same format, so the part keeps its name
                flagged = True
            elif suffix in _OOXML_SUFFIXES:
                part = _office(part, media_type, rw).data
                flagged = True
            elif not info.is_dir():
                part = _BLANK_SVG if suffix == ".svg" else b""
                flagged = True
            dst.writestr(zipfile.ZipInfo(info.filename, (1980, 1, 1, 0, 0, 0)), part)
    return Rebuilt(out.getvalue(), media_type, "rebuilt", flagged)


# ---------- PDF ----------


def _pdf(data: bytes, rw: Rewriter) -> Rebuilt:
    import pdfplumber  # heavy and optional: only the evals extra has it

    with pdfplumber.open(io.BytesIO(data)) as doc:
        pages = [page.extract_text() or "" for page in doc.pages]
    rw.learn(list[JsonValue](pages))
    if not any(p.strip() for p in pages):
        return Rebuilt(
            _write_pdf(["[redacted page]"] * max(1, len(pages))),
            "application/pdf",
            "redacted",
            True,
        )
    return Rebuilt(_write_pdf([rw.text(p) for p in pages]), "application/pdf", "rebuilt", True)


def _write_pdf(pages: list[str]) -> bytes:
    """A plain text PDF (Helvetica, one column): enough to carry a rebuilt text layer."""

    def lines(text: str) -> list[str]:
        wrapped = [
            part
            for line in text.splitlines() or [""]
            for part in re.findall(f".{{1,{_PDF_WIDTH}}}", line) or [""]
        ]
        return wrapped or [""]

    objects: list[bytes] = [b"<< /Type /Catalog /Pages 2 0 R >>", b""]
    kids: list[int] = []
    for text in pages:
        rows = lines(text)
        for start in range(0, len(rows), _PDF_LINES):
            chunk = rows[start : start + _PDF_LINES]
            esc = [r.encode("latin-1", "replace").decode("latin-1") for r in chunk]
            stream = (
                "BT /F1 10 Tf 12 TL 50 750 Td "
                + " T* ".join(
                    "(" + e.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)") + ") Tj"
                    for e in esc
                )
                + " ET"
            )
            body = stream.encode("latin-1")
            objects.append(b"<< /Length %d >>\nstream\n%s\nendstream" % (len(body), body))
            objects.append(
                b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents %d 0 R "
                b"/Resources << /Font << /F1 << /Type /Font /Subtype /Type1 "
                b"/BaseFont /Helvetica >> >> >> >>" % (len(objects))
            )
            kids.append(len(objects))
    objects[1] = b"<< /Type /Pages /Count %d /Kids [%s] >>" % (
        len(kids),
        b" ".join(b"%d 0 R" % k for k in kids),
    )
    out = io.BytesIO(b"%PDF-1.4\n")
    out.seek(0, io.SEEK_END)
    offsets: list[int] = []
    for number, obj in enumerate(objects, 1):
        offsets.append(out.tell())
        out.write(b"%d 0 obj\n%s\nendobj\n" % (number, obj))
    xref = out.tell()
    out.write(b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1))
    out.write(b"".join(b"%010d 00000 n \n" % o for o in offsets))
    out.write(
        b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref)
    )
    return out.getvalue()
