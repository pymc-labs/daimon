"""The shared pytest plugin keeps structlog's console tracebacks cheap and complete."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
import structlog
from daimon.testing.pytest_plugin import use_plain_console_tracebacks


@pytest.fixture
def default_structlog() -> Iterator[None]:
    """Run against structlog's built-in defaults, then restore whatever was configured."""
    previous = structlog.get_config()
    structlog.reset_defaults()
    use_plain_console_tracebacks()
    try:
        yield
    finally:
        structlog.configure(**previous)


def test_a_logged_exception_prints_a_plain_traceback_naming_the_failing_line(
    default_structlog: None, capsys: pytest.CaptureFixture[str]
) -> None:
    def fail() -> None:
        raise ValueError("mint failed")

    try:
        fail()
    except ValueError:
        structlog.get_logger().exception("refresh failed")

    output = capsys.readouterr().out
    assert "refresh failed" in output
    assert "Traceback (most recent call last)" in output
    assert 'raise ValueError("mint failed")' in output
    assert "ValueError: mint failed" in output
    assert "╭" not in output, "rich's boxed traceback is the slow renderer this plugin avoids"


def test_the_plain_formatter_survives_a_test_resetting_structlog(
    default_structlog: None,
) -> None:
    structlog.reset_defaults()

    renderers = [
        processor
        for processor in structlog.get_config()["processors"]
        if isinstance(processor, structlog.dev.ConsoleRenderer)
    ]
    assert renderers
    assert all(r.exception_formatter is structlog.dev.plain_traceback for r in renderers)
