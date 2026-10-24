"""Replay recorded Gemini interactions at their own time.

Recorded interactions are stamped 2026-10-10 to 2026-10-12, and the driver expires a
workspace seven days after its last activity, so replays run with the clock set just
after the last recording. Import `gemini_recorded_clock` into a `conftest.py`.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

GEMINI_RECORDED_AT = datetime(2026, 10, 13, tzinfo=UTC)


@pytest.fixture
def gemini_recorded_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("mux.drivers.gemini.core.now", lambda: GEMINI_RECORDED_AT)
