"""Gemini durable Records decode and encode run off the event loop."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest
from daimon.core.turn import gemini_state
from daimon.core.turn.gemini_state import SQLiteGeminiStorage
from mux.drivers.gemini import Records


@pytest.mark.asyncio
async def test_records_codec_runs_off_the_event_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A large body must not hold the loop: load and dump run on the codec pool."""
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
    async with storage.transaction():
        pass
    async with storage.transaction():
        pass
    assert len(threads) == 4, "each transaction loads then saves"
    assert all(name.startswith("gemini-records") for name in threads), (
        f"codec ran on {threads}, not the dedicated pool"
    )
