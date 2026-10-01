"""uvicorn's access log for the report host carries no reader or upload capability."""

from __future__ import annotations

import logging

from report_host.__main__ import uvicorn_log_config
from report_host.logs import RedactRequestTargets, redact_request_text


def test_access_record_target_is_redacted() -> None:
    record = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        1,
        '%s - "%s %s HTTP/%s" %d',
        ("127.0.0.1:1", "GET", "/r/q3-review?k=reader-secret-1&x=2", "1.1", 200),
        None,
    )

    RedactRequestTargets().filter(record)

    message = record.getMessage()
    assert "reader-secret-1" not in message
    assert "/r/q3-review?k=[redacted]&x=[redacted]" in message


def test_capability_paths_are_redacted() -> None:
    text = redact_request_text("PUT /upload/turn-secret-2 then PUT /publish/cap-secret-3")

    assert "turn-secret-2" not in text and "cap-secret-3" not in text


def test_log_config_installs_the_filter_on_every_handler() -> None:
    config = uvicorn_log_config()

    for handler in config["handlers"].values():
        assert "redact_request_targets" in handler["filters"]
    assert config["loggers"]["httpx"]["level"] == "WARNING"
