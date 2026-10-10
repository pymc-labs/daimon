from pathlib import Path

import pytest

from qa.live.locking import live_run_lock
from qa.live.types import Pending


def test_timer_and_manual_cannot_overlap_then_release(tmp_path: Path) -> None:
    path = tmp_path / "live.lock"
    with live_run_lock(path), pytest.raises(Pending, match="timer/manual"), live_run_lock(path):
        pytest.fail("overlapping run acquired lock")
    with live_run_lock(path):
        pass
