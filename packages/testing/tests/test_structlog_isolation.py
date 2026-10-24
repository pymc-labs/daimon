"""A test that reconfigures structlog leaves the next test's log capture working."""

from __future__ import annotations

import structlog
from daimon.core.logging_setup import configure_log_level
from daimon.testing.pytest_plugin import restoring_structlog_config


def _captures_debug() -> bool:
    with structlog.testing.capture_logs() as logs:
        structlog.get_logger().debug("probe")
    return {"event": "probe", "log_level": "debug"} in logs


def test_production_logging_setup_does_not_outlive_its_test() -> None:
    assert _captures_debug()
    with restoring_structlog_config():
        configure_log_level("INFO")  # what create_mcp_app's configure_logging does
        assert not _captures_debug(), "the INFO filter drops debug events before capture"
    assert _captures_debug(), "the next test captures debug events again"


def test_a_configuration_set_before_the_test_is_kept() -> None:
    with restoring_structlog_config():
        structlog.configure(processors=[structlog.processors.KeyValueRenderer()])
        before = structlog.get_config()
        with restoring_structlog_config():
            configure_log_level("INFO")
        assert structlog.get_config() == before
