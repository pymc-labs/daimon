"""Tests for send_message's file_handles parameter (FileStore-backed attachments)."""

from __future__ import annotations

from pathlib import Path

import pytest
from daimon.adapters.mcp.file_store import FileStore
from daimon.adapters.mcp.tools.discord import (
    _build_files_from_handles,  # pyright: ignore[reportPrivateUsage]
)


@pytest.mark.asyncio
async def test_build_files_from_handles_reads_filestore_bytes(tmp_path: Path) -> None:
    store = FileStore(base_dir=tmp_path)
    handle = store.put(data=b"FAKEMP3", mime_type="audio/mpeg", title="brief")
    files = await _build_files_from_handles([handle.id], store=store)
    assert len(files) == 1
    fp = files[0].fp
    fp.seek(0)
    assert fp.read() == b"FAKEMP3"
    assert files[0].filename == "brief.mp3", (
        "Discord attachment filename must be the user-visible display name "
        "(derived from title), not the server-minted handle id"
    )


@pytest.mark.asyncio
async def test_build_files_from_handles_raises_with_filename_in_message(
    tmp_path: Path,
) -> None:
    """Missing filename surfaces a ToolError whose message names the file —
    agents debug from this string. Spike 028 locked this contract."""
    from fastmcp.exceptions import ToolError

    store = FileStore(base_dir=tmp_path)
    with pytest.raises(ToolError, match="nope.mp3"):
        await _build_files_from_handles(["nope.mp3"], store=store)


@pytest.mark.asyncio
async def test_build_files_from_handles_rejects_too_many(tmp_path: Path) -> None:
    from fastmcp.exceptions import ToolError

    store = FileStore(base_dir=tmp_path)
    # 11 names is over the cap; doesn't matter that the files don't exist —
    # the cap check fires first.
    with pytest.raises(ToolError, match="max 10"):
        await _build_files_from_handles([f"f{i}.mp3" for i in range(11)], store=store)
