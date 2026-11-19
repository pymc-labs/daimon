"""Large Gemini durable Records decode and encode off the event loop."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest
from daimon.core.turn import gemini_state
from daimon.core.turn.gemini_state import SQLiteGeminiStorage
from mux.drivers.gemini import Records


@pytest.mark.asyncio
async def test_only_large_records_use_the_codec_pool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A small body decodes on the loop; a large one loads and saves on the codec pool."""
    threads: list[str] = []
    load, dump = gemini_state._load, gemini_state._dump  # pyright: ignore[reportPrivateUsage]

    def traced_load(body: str | None) -> Records:
        threads.append(threading.current_thread().name)
        return load(body)

    def traced_dump(records: Records) -> str:
        threads.append(threading.current_thread().name)
        return dump(records)

    monkeypatch.setattr(gemini_state, "_load", traced_load)
    monkeypatch.setattr(gemini_state, "_dump", traced_dump)
    storage = SQLiteGeminiStorage(tmp_path / "records.sqlite", namespace="ns")
    async with storage.transaction() as records:
        records.creations["t", "a", "first"] = "p", "digest", "id"
    assert threads == ["MainThread", "MainThread"], "a small body loads and saves inline"
    threads.clear()
    monkeypatch.setattr(gemini_state, "POOL_BYTES", 1)
    async with storage.transaction() as records:
        records.creations["t", "a", "second"] = "p", "digest", "id"
    assert len(threads) == 2 and all(n.startswith("gemini-records") for n in threads), (
        f"a large body ran on {threads}, not the dedicated pool"
    )
