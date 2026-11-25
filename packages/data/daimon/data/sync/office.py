# pyright: basic
# ruff: noqa: E501
"""The words of an Office file (docx, xlsx, pptx), read from its XML parts with bounds on everything a hostile file can make big.
Standard library only: office_child.py imports this module alone, in a bare interpreter."""

import io
import re
import struct
import zipfile
from xml.etree import ElementTree

DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
PPTX = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
TEXT_CAP = 200_000  # characters kept of one item
UNZIPPED_CAP = 10_000_000  # declared unpacked bytes of the parts one Office file parses: one XML text node can fill a part
DEPTH_CAP = 256  # elements open at once in one part (Office: a few dozen); open ones are kept whole
NAMES_CAP = 2_000  # distinct element and attribute names one part may use (Office: a few hundred); the parser keeps them
TAG_CAP = 65_536  # bytes of one tag: a start tag is built whole, every attribute with it, before its event
ENTRIES_CAP = 10_000  # entries of an Office file (a few hundred): the zip reader loads them all before any part
DIRECTORY_CAP = 2_000_000  # bytes of its central directory, which the zip reader loads whole whatever the count says
LONG_TAG = re.compile(rb"<[^>]{%d}" % TAG_CAP)
MANY_ATTRS = re.compile(
    rb"""(?:(?<=\S)\s+[^\s=<>"']{1,256}\s*=\s*(?:"[^"]{0,1024}"|'[^']{0,1024}')){256}"""
)  # in a row: Office
# tags carry under 30. One try per whitespace run (the look-behind) and bounded values keep the scan linear.
LONG_NS = re.compile(
    rb"xmlns[^=\s>]*\s*=\s*[\"'][^\"']{256}"
)  # a namespace far longer than any Office one: every name carries it


def unsafe(z: zipfile.ZipFile, info: zipfile.ZipInfo) -> bool:
    """Whether a part is not UTF-8 XML (Office writes UTF-8; UTF-8 XML never holds a NUL byte, UTF-16 and UTF-32 markup always
    does, and a wider encoding would hide a declaration from the byte scan) or has a document type or entity declaration, read
    in chunks."""
    tail, tag = b"", b""  # the last bytes, and an unclosed tag carried over
    with z.open(info) as f:
        while chunk := f.read(1 << 16):
            near, buf = tail + chunk, tag + chunk
            if (
                b"\x00" in chunk
                or b"<!DOCTYPE" in near
                or b"<!ENTITY" in near
                or LONG_NS.search(near)
                or LONG_TAG.search(buf)
                or MANY_ATTRS.search(buf)
            ):
                return True
            tail, tag = (
                chunk[-300:],
                buf[buf.rfind(b"<") :] if buf.rfind(b"<") > buf.rfind(b">") else b"",
            )
    return False


def entries(blob: bytes) -> int | None:
    """The entry count its end record gives, before the zip reader loads them; None: no end record, a ZIP64 one, or a central
    directory over DIRECTORY_CAP bytes (the reader loads the directory by its size, not by the count)."""
    at = blob.rfind(b"PK\x05\x06", max(0, len(blob) - 65_557))
    if at < 0 or len(blob) < at + 22 or blob.rfind(b"PK\x06\x07", max(0, at - 20), at) >= 0:
        return None
    n, size = struct.unpack("<HI", blob[at + 10 : at + 16])
    return None if n == 0xFFFF or size > DIRECTORY_CAP else n


def office_text(mime: str, blob: bytes) -> str:
    """The words of a docx, pptx or xlsx: its XML parts, in order, without markup. Streamed, element by element, and stopped at
    TEXT_CAP: an unpacked part can be many times the file's size. Parts are read while their declared unpacked sizes stay within
    UNZIPPED_CAP (a stored or deflated part never unpacks past its declared size): one element's text is held whole, and a small
    file can declare one of tens of MB. A file of more than ENTRIES_CAP entries raises (read() keeps it metadata only)."""
    parts = {
        DOCX: r"word/document\.xml",
        PPTX: r"ppt/slides/slide\d+\.xml",
        XLSX: r"xl/(sharedStrings|worksheets/sheet\d+)\.xml",
    }[mime]
    out, size, room = [], 0, UNZIPPED_CAP
    if (n := entries(blob)) is None or n > ENTRIES_CAP:
        raise ValueError("not an Office file this reads")
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        for info in sorted(
            (i for i in z.infolist() if re.fullmatch(parts, i.filename)), key=lambda i: i.filename
        ):
            if info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
                continue  # Office writes deflate; bzip2 and lzma unpack without a bound before the size is checked
            if info.file_size > room:
                break
            room -= info.file_size
            if unsafe(z, info):
                continue  # Office XML declares no entities; an entity can expand far past TEXT_CAP before it applies
            words, open_, names = [], [], set()
            with z.open(info) as f:
                parser = ElementTree.XMLParser(
                    encoding="utf-8"
                )  # as scanned: a declared encoding is not honoured
                for event, el in ElementTree.iterparse(f, ("start", "end"), parser=parser):
                    if event == "start":
                        open_.append(el)
                        names.add(el.tag)
                        names.update(el.attrib)
                        if len(names) > NAMES_CAP or len(open_) > DEPTH_CAP:
                            break
                        continue
                    open_.pop()
                    if el.tag.endswith("}t") and el.text:
                        words.append(el.text)
                        size += len(el.text) + 1
                    el.clear()
                    if open_ and len(open_[-1]) and open_[-1][0] is el:
                        del open_[
                            -1
                        ][
                            0
                        ]  # done with it (siblings end in order): memory follows the depth and one read chunk
                    if size >= TEXT_CAP:
                        break
            out.append(" ".join(words))
            if size >= TEXT_CAP:
                break
    return "\n".join(out)
