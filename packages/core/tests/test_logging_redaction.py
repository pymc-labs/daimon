"""Rendered log output never carries credential text from exceptions or fields."""

from __future__ import annotations

import json
import logging
import uuid

import pytest
import structlog
from daimon.core.logging_setup import configure_log_level
from daimon.core.observability import LogRedactionFilter, install_log_redaction


def _canary() -> str:
    return "canary-" + uuid.uuid4().hex


def test_json_logging_redacts_exception_text_and_fields(capsys: pytest.CaptureFixture[str]) -> None:
    canary = _canary()
    configure_log_level("INFO")
    log = structlog.get_logger("daimon.test")
    try:
        raise RuntimeError(json.dumps({"api_key": canary}))
    except RuntimeError:
        log.exception("connector.failed", detail=f"token={canary}")

    out = capsys.readouterr().out
    assert "connector.failed" in out
    assert "RuntimeError" in out
    assert canary not in out


def test_stdlib_records_and_tracebacks_are_redacted(caplog: pytest.LogCaptureFixture) -> None:
    canary = _canary()
    handler_output: list[str] = []

    class _Collect(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            handler_output.append(self.format(record))

    logger = logging.getLogger("uvicorn.error")
    handler = _Collect()
    handler.addFilter(LogRedactionFilter())
    logger.addHandler(handler)
    try:
        try:
            raise RuntimeError(f"Authorization: Bearer {canary}")
        except RuntimeError:
            logger.exception("Exception in ASGI application %s", f"password={canary}")
    finally:
        logger.removeHandler(handler)

    rendered = "\n".join(handler_output)
    assert "Exception in ASGI application" in rendered
    assert "RuntimeError" in rendered
    assert canary not in rendered


def test_access_records_keep_positional_args_with_a_redacted_target() -> None:
    token = "Ab3" + uuid.uuid4().hex + "Zz9"
    record = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        1,
        '%s - "%s %s HTTP/%s" %d',
        ("127.0.0.1:1", "GET", f"/slack/file/{token}?code=abc&next=/x#frag", "1.1", 200),
        None,
    )

    LogRedactionFilter().filter(record)

    message = record.getMessage()
    assert token not in message
    assert "abc" not in message and "frag" not in message
    assert "/slack/file/[redacted]?code=" in message
    assert message.endswith("200")


def test_install_log_redaction_quiets_httpx_and_is_idempotent() -> None:
    install_log_redaction()
    install_log_redaction()

    access = logging.getLogger("uvicorn.access")
    assert sum(isinstance(f, LogRedactionFilter) for f in access.filters) == 1
    assert logging.getLogger("httpx").getEffectiveLevel() >= logging.WARNING


def test_json_logging_redacts_nested_fields_secret_names_and_object_reprs(
    capsys: pytest.CaptureFixture[str],
) -> None:
    canary = _canary()

    class _Opaque:
        def __repr__(self) -> str:
            return f"Opaque(password={canary})"

    configure_log_level("INFO")
    structlog.get_logger("daimon.test").info(
        "call.done",
        detail={"inner": [{"access_token": canary}], "note": f"token={canary}"},
        bot_token=canary,
        obj=_Opaque(),
        count=3,
    )

    out = capsys.readouterr().out
    assert "call.done" in out and '"count": 3' in out
    assert canary not in out


def test_free_text_bare_query_keys_and_relative_targets_are_redacted() -> None:
    from daimon.core.observability import redact_log_text

    canary = _canary()
    for text in (
        f'127.0.0.1 - "GET /oauth/callback?code={canary}&state=x HTTP/1.1" 200',
        f"redirect to /cb?{canary}",
        f"https://h/p?{canary}&a=1",
    ):
        assert canary not in redact_log_text(text), text
    assert redact_log_text("what? really?") == "what? really?"


def test_rich_handler_tracebacks_are_redacted_and_carry_no_locals(
    capfd: pytest.CaptureFixture[str],
) -> None:
    """A library logger with its own rich handler (fastmcp's) can't bypass redaction."""
    from rich.logging import RichHandler

    text_canary, local_canary = _canary(), _canary()
    logger = logging.getLogger("fastmcp.test_rich")
    parent = logging.getLogger("fastmcp")
    handler = RichHandler(rich_tracebacks=True, tracebacks_show_locals=True)
    parent.addHandler(handler)
    try:
        install_log_redaction()
        held_secret = local_canary  # noqa: F841 - a frame local that must not print
        try:
            raise RuntimeError(f"Authorization: Bearer {text_canary}")
        except RuntimeError:
            logger.exception("Error calling tool")
    finally:
        parent.removeHandler(handler)

    out = capfd.readouterr()
    logged = out.out + out.err
    assert "Error calling tool" in logged
    assert text_canary not in logged
    assert local_canary not in logged
