"""Reading a file in chunks gives back the file, byte for byte, whatever it holds."""

from __future__ import annotations

import base64
import hashlib
from pathlib import Path

import pytest
from compute_session import tools

#: Characters of one, two, three and four bytes, in an order that never lines up with a chunk.
TEXT = "aé€😀" * 7 + "\n" + "😀€é" * 5 + "日本語 naïve\n"


def read_all(path: Path, limit: int) -> tuple[bytes, list[tools.Result]]:
    """Follow ``offset + bytes`` to the end; each chunk must hold exactly ``bytes`` bytes."""
    data, chunks, offset = b"", list[tools.Result](), 0
    while True:
        chunk = tools.read_file(str(path), offset, limit)
        content, used = chunk["content"], chunk["bytes"]
        assert isinstance(content, str) and isinstance(used, int), chunk
        raw = content.encode() if chunk["encoding"] == "utf-8" else base64.b64decode(content)
        assert chunk["encoding"] in {"utf-8", "base64"} and len(raw) == used
        assert used > 0 or chunk["eof"], "a chunk that reads nothing would never end"
        assert chunk["offset"] == offset
        data, offset = data + raw, offset + used
        chunks.append(chunk)
        if chunk["eof"]:
            return data, chunks


def cuts_inside_characters(text: str) -> dict[int, tuple[int, int]]:
    """Each byte offset that falls inside a character: (character width, bytes before the cut)."""
    cuts: dict[int, tuple[int, int]] = {}
    position = 0
    for character in text:
        width = len(character.encode())
        cuts.update({position + before: (width, before) for before in range(1, width)})
        position += width
    return cuts


def test_text_read_at_every_chunk_size_is_the_same_text(tmp_path: Path) -> None:
    path = tmp_path / "text.txt"
    path.write_text(TEXT, encoding="utf-8")
    size = len(TEXT.encode())
    cuts = cuts_inside_characters(TEXT)
    seen: set[tuple[int, int]] = set()
    for limit in range(4, size + 2):
        data, chunks = read_all(path, limit)
        assert data == TEXT.encode(), limit
        assert {chunk["encoding"] for chunk in chunks} == {"utf-8"}, limit
        assert "".join(str(chunk["content"]) for chunk in chunks) == TEXT, limit
        for chunk in chunks:
            used = chunk["bytes"]
            assert isinstance(used, int) and limit - 4 < used <= limit or chunk["eof"], chunk
        # Where a plain cut at offset + limit would have fallen inside a character.
        ends = [chunk["offset"] + limit for chunk in chunks if isinstance(chunk["offset"], int)]
        seen |= {cuts[end] for end in ends if end in cuts}
    # A cut after every byte of every character width was met and moved to the character's start.
    assert seen == {(2, 1), (3, 1), (3, 2), (4, 1), (4, 2), (4, 3)}


@pytest.mark.parametrize("limit", [1, 2, 3])
def test_a_limit_smaller_than_a_character_still_reads_the_file(tmp_path: Path, limit: int) -> None:
    path = tmp_path / "text.txt"
    path.write_text(TEXT, encoding="utf-8")
    data, chunks = read_all(path, limit)
    assert data == TEXT.encode()
    assert "base64" in {chunk["encoding"] for chunk in chunks}, "a piece of a character is not text"


def test_bytes_that_are_not_text_come_back_whole_as_base64(tmp_path: Path) -> None:
    path = tmp_path / "data.bin"
    content = bytes(range(256)) * 300 + "then text €".encode() + b"\xff"
    path.write_bytes(content)
    for limit in (100, 65536):
        data, chunks = read_all(path, limit)
        assert hashlib.sha256(data).digest() == hashlib.sha256(content).digest(), limit
        assert {chunk["encoding"] for chunk in chunks} >= {"base64"}
    first = tools.read_file(str(path), 0, 100)
    assert (first["encoding"], first["content"]) == ("utf-8", bytes(range(100)).decode()), (
        "a chunk that is text stays text, also in a file that is not"
    )


def test_a_character_cut_off_by_the_end_of_the_file(tmp_path: Path) -> None:
    path = tmp_path / "cut.txt"
    path.write_bytes(b"ab\xe2\x82")
    text = tools.read_file(str(path), 0, 65536)
    assert (text["encoding"], text["content"], text["bytes"], text["eof"]) == (
        "utf-8",
        "ab",
        2,
        False,
    )
    rest = tools.read_file(str(path), 2, 65536)
    assert (rest["encoding"], rest["content"], rest["bytes"], rest["eof"]) == (
        "base64",
        base64.b64encode(b"\xe2\x82").decode(),
        2,
        True,
    )


def test_a_chunk_shrunk_to_fit_the_response_still_ends_on_a_character(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tools, "MAX_RESULT_BYTES", 4000)
    path = tmp_path / "euros.txt"
    path.write_text("€" * 3000, encoding="utf-8")
    data, chunks = read_all(path, 9000)
    assert data == ("€" * 3000).encode() and len(chunks) > 1
    for chunk in chunks:
        used = chunk["bytes"]
        assert isinstance(used, int) and chunk["content"] == "€" * (used // 3), chunk
        assert len(tools.encode(tools.tool_result(chunk))) <= 4000
