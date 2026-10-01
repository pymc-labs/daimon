"""Tests for daimon.testing.archives.make_tarball."""

from __future__ import annotations

import io
import tarfile

from daimon.testing.archives import make_tarball


def _entries(blob: bytes) -> dict[str, tuple[bytes, int]]:
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as archive:
        out: dict[str, tuple[bytes, int]] = {}
        for member in archive.getmembers():
            extracted = archive.extractfile(member)
            assert extracted is not None, f"{member.name} must be a regular file"
            out[member.name] = (extracted.read(), int(member.mtime))
        return out


def test_make_tarball_holds_the_given_files_in_order() -> None:
    blob = make_tarball({"repo/SKILL.md": b"---\nname: s\n---\n", "repo/a.py": b"print()\n"})
    entries = _entries(blob)
    assert list(entries) == ["repo/SKILL.md", "repo/a.py"], "entries keep mapping order"
    assert entries["repo/SKILL.md"][0] == b"---\nname: s\n---\n", "content must round-trip"
    assert entries["repo/a.py"][1] == 0, "without mtime= entries keep tarfile's zero default"


def test_make_tarball_is_deterministic_and_stamps_mtime_when_given() -> None:
    files = {"x": b"1"}
    assert _entries(make_tarball(files)) == _entries(make_tarball(files)), (
        "two calls with the same input must produce the same entries"
    )
    assert _entries(make_tarball(files, mtime=1_700_000_000))["x"][1] == 1_700_000_000, (
        "mtime= must stamp every entry"
    )
