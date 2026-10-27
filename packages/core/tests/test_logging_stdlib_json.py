"""Stdlib log records, shutdown flag and uncaught exceptions render as one JSON line each."""

from __future__ import annotations

import io
import json
import logging
import sys
import threading
from collections.abc import Iterator

import pytest
import structlog
from daimon.core.logging_setup import (
    clear_shutting_down,
    configure_log_level,
    configure_logging,
    install_excepthooks,
    json_line_formatter,
    mark_shutting_down,
)
from daimon.core.observability import install_log_redaction


@pytest.fixture(autouse=True)
def isolated_process_state(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(sys, "excepthook", sys.excepthook)
    monkeypatch.setattr(threading, "excepthook", threading.excepthook)
    clear_shutting_down()
    yield
    clear_shutting_down()
    structlog.reset_defaults()


@pytest.fixture
def stdlib_lines() -> Iterator[tuple[logging.Logger, io.StringIO]]:
    """A logger whose handler renders like the one `configure_logging` installs."""
    install_log_redaction()
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(json_line_formatter())
    logger = logging.getLogger("daimon.test.stdlib_json")
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    try:
        yield logger, stream
    finally:
        logger.removeHandler(handler)


def _only_line(stream: io.StringIO) -> dict[str, object]:
    lines = stream.getvalue().splitlines()
    assert len(lines) == 1, f"one record must be one line, got {lines!r}"
    return json.loads(lines[0])


def test_stdlib_record_is_one_json_line_with_contract_fields(
    stdlib_lines: tuple[logging.Logger, io.StringIO],
) -> None:
    logger, stream = stdlib_lines

    logger.warning("connection to %s dropped (100%% sure)", "gateway")

    record = _only_line(stream)
    assert record["event"] == "connection to gateway dropped (100% sure)"
    assert record["logger"] == "daimon.test.stdlib_json"
    assert record["level"] == "warning"
    assert record["severity"] == "WARNING"
    assert str(record["timestamp"]).endswith("Z")
    assert "exception" not in record
    assert "shutting_down" not in record


@pytest.mark.parametrize(
    ("level", "severity"),
    [
        (logging.DEBUG, "DEBUG"),
        (logging.INFO, "INFO"),
        (logging.WARNING, "WARNING"),
        (logging.ERROR, "ERROR"),
        (logging.CRITICAL, "CRITICAL"),
    ],
)
def test_stdlib_severity_follows_the_level(
    stdlib_lines: tuple[logging.Logger, io.StringIO], level: int, severity: str
) -> None:
    logger, stream = stdlib_lines

    logger.log(level, "line")

    assert _only_line(stream)["severity"] == severity


def test_stdlib_exception_is_one_line_with_a_structured_exception(
    stdlib_lines: tuple[logging.Logger, io.StringIO],
) -> None:
    logger, stream = stdlib_lines

    try:
        raise ValueError("boom\nsecond line")
    except ValueError:
        logger.exception("task failed")

    rendered = stream.getvalue()
    assert rendered.count("\n") == 1, "a traceback must not spill onto more lines"
    assert "Traceback (most recent call last)" not in rendered
    record = json.loads(rendered)
    assert record["event"] == "task failed"
    assert record["severity"] == "ERROR"
    exception = record["exception"]
    assert isinstance(exception, list)
    assert exception[0]["exc_type"] == "ValueError"
    assert exception[0]["frames"], "stack locations stay available"
    assert not any(frame.get("locals") for frame in exception[0]["frames"])


def test_stdlib_exception_text_and_message_are_still_redacted(
    stdlib_lines: tuple[logging.Logger, io.StringIO],
) -> None:
    logger, stream = stdlib_lines
    secret = "sk-ant-api03-" + "a1B2c3D4e5F6" * 4

    try:
        raise RuntimeError(f"upstream said api_key={secret}")
    except RuntimeError:
        logger.exception("call failed with token=%s", secret)

    rendered = stream.getvalue()
    assert secret not in rendered
    assert rendered.count("\n") == 1
    json.loads(rendered)


def test_shutting_down_is_flagged_on_every_later_line(
    stdlib_lines: tuple[logging.Logger, io.StringIO], capsys: pytest.CaptureFixture[str]
) -> None:
    logger, stream = stdlib_lines
    configure_log_level("INFO")
    structlog.get_logger().info("before")
    logger.info("before")
    before = json.loads(capsys.readouterr().out)
    assert "shutting_down" not in before
    assert "shutting_down" not in _only_line(stream)

    mark_shutting_down()
    stream.seek(0)
    stream.truncate()
    structlog.get_logger().info("after")
    logger.info("after")

    assert json.loads(capsys.readouterr().out)["shutting_down"] is True
    assert _only_line(stream)["shutting_down"] is True


def test_uncaught_exception_in_the_main_thread_is_logged_critical(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_log_level("INFO")
    install_excepthooks()

    try:
        raise RuntimeError("fatal")
    except RuntimeError:
        sys.excepthook(*sys.exc_info())  # pyright: ignore[reportArgumentType]

    (line,) = capsys.readouterr().out.splitlines()
    record = json.loads(line)
    assert record["event"] == "process.uncaught_exception"
    assert record["level"] == "critical"
    assert record["severity"] == "CRITICAL"
    assert record["exception"][0]["exc_type"] == "RuntimeError"


def test_uncaught_exception_in_a_thread_is_logged_but_system_exit_is_not(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_log_level("INFO")
    install_excepthooks()

    def fail() -> None:
        raise KeyError("worker")

    def leave() -> None:
        raise SystemExit(0)

    for target in (fail, leave):
        worker = threading.Thread(target=target)
        worker.start()
        worker.join()

    (line,) = capsys.readouterr().out.splitlines()
    record = json.loads(line)
    assert record["event"] == "process.uncaught_exception"
    assert record["exception"][0]["exc_type"] == "KeyError"


def test_configure_logging_renders_library_output_and_uvicorn_as_json(
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = logging.getLogger()
    access = logging.getLogger("uvicorn.access")
    saved_root, saved_access = root.handlers[:], access.handlers[:]
    saved_level = access.level
    access.setLevel(logging.INFO)  # uvicorn sets this when it configures logging
    uvicorn_stream = io.StringIO()
    access.addHandler(logging.StreamHandler(uvicorn_stream))
    root.handlers.clear()  # as in a deployment: nothing else is attached yet
    try:
        configure_logging("INFO")
        try:
            raise OSError("socket closed")
        except OSError:
            logging.getLogger("discord.client").exception("Attempting a reconnect")
        access.info(
            '%s - "%s %s HTTP/%s" %d',
            "10.0.0.1:5000",
            "GET",
            "/uploads/abcdefghijklmnopqrstuvwxyz0123456789",
            "1.1",
            200,
        )
    finally:
        added = [h for h in root.handlers if h not in saved_root]
        for handler in added:
            root.removeHandler(handler)
        root.handlers[:] = saved_root
        access.handlers[:] = saved_access
        access.setLevel(saved_level)

    (library_line,) = capsys.readouterr().err.splitlines()
    library = json.loads(library_line)
    assert library["event"] == "Attempting a reconnect"
    assert library["logger"] == "discord.client"
    assert library["exception"][0]["exc_type"] == "OSError"
    uvicorn_record = json.loads(uvicorn_stream.getvalue())
    assert uvicorn_record["logger"] == "uvicorn.access"
    assert uvicorn_record["severity"] == "INFO"
    assert "abcdefghijklmnopqrstuvwxyz0123456789" not in uvicorn_stream.getvalue()
