"""Rebuilding blobs with fakes: text, JSON, Office files, PDFs with a text layer and images.

`rebuild` returns the new bytes and how they were made, or None when it cannot handle the type
(the sweep then flags the blob). The same code with a counting rewriter finds leftovers.
"""

from __future__ import annotations

import io
import json
import posixpath
import re
import urllib.parse
import zipfile
from collections.abc import Mapping
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
# Attributes people write in: alt text, titles and tooltips get name detection too.
_PROSE_ATTRS: Final = frozenset({"descr", "title", "tooltip", "alt"})
_PATH_ATTRS: Final = frozenset({"Target", "PartName"})
_MAX_PART: Final = 64 * 1024 * 1024
_MAX_UNPACKED: Final = 256 * 1024 * 1024
_MAX_PARTS: Final = 10_000
_MAX_DEPTH: Final = 3
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


@dataclass(frozen=True)
class _Xml:
    data: bytes
    root: ET.Element
    paragraphs: list[tuple[list[ET.Element], str]]
    inside: set[int]

    def strings(self) -> list[JsonValue]:
        """Everything a person may have written in the part, paragraphs joined across runs."""
        found: list[JsonValue] = [joined for _, joined in self.paragraphs]
        for el in self.root.iter():
            if id(el) not in self.inside:
                found += [t for t in (el.text, el.tail) if t and t.strip()]
            found += list(el.attrib.values())
        return found


def _parse_xml(data: bytes) -> _Xml:
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
    return _Xml(data, root, paragraphs, inside)


def _rewrite_xml(xml: _Xml, rw: Rewriter, renamed: Mapping[str, str], name: str) -> bytes:
    data, root, paragraphs, inside = xml.data, xml.root, xml.paragraphs, xml.inside
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
        if el.tail and el.tail.strip():
            el.tail = rw.text(el.tail)
        for key, val in el.attrib.items():
            attr = key.rsplit("}", 1)[-1]
            if attr in _AUTHOR_ATTRS:
                el.set(key, _AUTHOR_ATTRS[attr])
                continue
            if attr in _PATH_ATTRS and el.get("TargetMode") != "External":
                val = _retarget(name, val, renamed)
            el.set(key, rw.text(val, names=attr in _PROSE_ATTRS))
    head = re.match(rb"(<\?xml[^>]*\?>\s*)?", data)
    prolog = head.group(0) if head else b""
    body = ET.tostring(root, encoding="unicode")
    original = re.search(r"<[^?!/][^>]*>", data[len(prolog) :].decode("utf-8"))
    rebuilt = re.match(r"<[^>]*>", body)
    if original and rebuilt and not rebuilt.group().endswith("/>"):
        body = original.group() + body[rebuilt.end() :]  # keep every declared namespace
    return prolog + body.encode()


@dataclass
class _Budget:
    """What one Office file may unpack, embedded files included."""

    parts: int = _MAX_PARTS
    size: int = _MAX_UNPACKED

    def take(self, infos: list[zipfile.ZipInfo]) -> None:
        self.parts -= len(infos)
        self.size -= sum(i.file_size for i in infos)
        if self.parts < 0 or self.size < 0 or any(i.file_size > _MAX_PART for i in infos):
            raise ValueError("Office file too large to rebuild")


def _xml_part(name: str) -> bool:
    return bool(_XML_PARTS.search(name)) or name == "[Content_Types].xml"


def _retarget(part: str, target: str, renamed: Mapping[str, str]) -> str:
    """`target` (a relationship target or a content type's part name) after renaming."""
    if target.startswith("/"):
        path = target[1:]
    else:
        source = PurePosixPath(part).parent.parent if part.endswith(".rels") else ""
        path = posixpath.normpath(posixpath.join(str(source), target))
    if (new := renamed.get(urllib.parse.unquote(path))) is None:
        return target
    head, slash, _ = target.rpartition("/")
    return f"{head}{slash}{new}"


def _office(
    data: bytes, media_type: str, rw: Rewriter, budget: _Budget | None = None, depth: int = 0
) -> Rebuilt:
    """Every part rewritten: XML with fakes, images redacted, embedded Office files rebuilt the
    same way, and every other binary (OLE objects, EMF, SVG, fonts) blanked: none is kept.
    Binary parts are renamed (`media/part3.png`), since file names name people too."""
    if depth > _MAX_DEPTH:
        raise ValueError("Office files nested too deep")
    budget = budget or _Budget()
    out = io.BytesIO()
    flagged = False
    with (
        zipfile.ZipFile(io.BytesIO(data)) as src,
        zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as dst,
    ):
        infos = src.infolist()
        budget.take(infos)
        renamed = {
            i.filename: f"part{n}{PurePosixPath(i.filename).suffix}"
            for n, i in enumerate(infos)
            if not i.is_dir() and not _xml_part(i.filename)
        }
        xml = {i.filename: _parse_xml(src.read(i)) for i in infos if _xml_part(i.filename)}
        # A name found anywhere in the file is replaced everywhere in it.
        rw.learn([t for x in xml.values() for t in x.strings()])
        for info in infos:
            name = info.filename
            suffix = PurePosixPath(name).suffix.lower()
            if name in xml:
                part = _rewrite_xml(xml.pop(name), rw, renamed, name)
            elif suffix in _IMAGE_SUFFIXES:
                part = redact_image(src.read(info), rw)[0]  # same format, so the suffix stays
                flagged = True
            elif suffix in _OOXML_SUFFIXES:
                part = _office(src.read(info), media_type, rw, budget, depth + 1).data
                flagged = True
            else:
                part = _BLANK_SVG if suffix == ".svg" and not info.is_dir() else b""
                flagged = flagged or not info.is_dir()
            if name in renamed:
                name = str(PurePosixPath(name).with_name(renamed[name]))
            dst.writestr(zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0)), part)
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
