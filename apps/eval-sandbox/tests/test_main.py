"""The pod entry point's agent environment."""

from __future__ import annotations

import glob
from datetime import UTC, datetime

import pytest
from daimon_eval_sandbox import __main__ as entry


def _libfaketime(pattern: str) -> list[str]:
    return ["/lib/libfaketime.so.1"]


def test_case_clock_is_an_offset_that_keeps_running(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(glob, "glob", _libfaketime)
    now = datetime(2026, 1, 2, tzinfo=UTC).timestamp()
    env = entry.agent_env({"clock": "2026-01-01 00:00:00", "agent_env": {"A": "1"}}, now)
    assert env["FAKETIME"] == "-86400", "a relative offset, so each process's clock advances"
    assert "FAKETIME_DONT_RESET" not in env and env["A"] == "1", "case vars kept"
