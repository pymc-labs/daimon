"""Unit tests for the OB-1 production logging chain.

`configure_log_level` installs structlog's full JSON chain at a process
entrypoint. These tests prove the rendered JSON shape (rid/tenant_id from
contextvars, level, iso-utc timestamp, structured exception array) and that
`DAIMON_LOG__LEVEL` filtering is effective. No DB, no I/O beyond the captured
stdout the chain writes through `PrintLoggerFactory`.
"""

from __future__ import annotations

import json
from collections.abc import Iterator

import pytest
import structlog
from daimon.core.logging_setup import configure_log_level


@pytest.fixture(autouse=True)
def reset_structlog() -> Iterator[None]:
    """Each test reconfigures structlog globally; restore defaults + clear any
    bound contextvars afterward so other tests in the suite are unaffected."""
    yield
    structlog.reset_defaults()
    structlog.contextvars.clear_contextvars()


def test_chain_renders_json_with_contextvars_level_timestamp_and_exception_array(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_log_level("INFO")
    log = structlog.get_logger()

    structlog.contextvars.bind_contextvars(rid="r1", tenant_id="t1")
    try:
        try:
            raise ValueError("boom")
        except ValueError:
            log.exception("turn.failed", guild_id="g1")
    finally:
        structlog.contextvars.unbind_contextvars("rid", "tenant_id")

    captured = capsys.readouterr().out.strip()
    assert captured, "the chain should write exactly one JSON line to stdout"
    record = json.loads(captured)

    assert record["event"] == "turn.failed", "event key carries the log message"
    assert record["rid"] == "r1", "merge_contextvars should inject the bound rid"
    assert record["tenant_id"] == "t1", "merge_contextvars should inject the bound tenant_id"
    assert record["guild_id"] == "g1", "per-call kwargs flow into the JSON line"
    assert record["level"] == "error", "log.exception renders at error level via add_log_level"
    assert record["timestamp"].endswith("Z"), (
        "TimeStamper(iso, utc) should emit a trailing-Z UTC timestamp"
    )
    assert isinstance(record["exception"], list), (
        "dict_tracebacks should emit a JSON exception array, not a string"
    )
    assert record["exception"][0]["exc_type"] == "ValueError", (
        "the structured exception frame should name the raised type"
    )


def test_debug_level_admits_debug_lines(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_log_level("DEBUG")
    log = structlog.get_logger()

    log.debug("debug.line", k="v")

    captured = capsys.readouterr().out.strip()
    assert captured, "DEBUG level should admit a .debug() line"
    record = json.loads(captured)
    assert record["event"] == "debug.line", "the debug line should render as JSON"
    assert record["level"] == "debug", "the rendered level should be debug"


def test_warning_level_filters_info_lines(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_log_level("WARNING")
    log = structlog.get_logger()

    log.info("info.line", k="v")

    captured = capsys.readouterr().out.strip()
    assert captured == "", "WARNING level should filter out a .info() line — nothing emitted"
