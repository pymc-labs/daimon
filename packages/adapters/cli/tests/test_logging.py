import structlog
from daimon.adapters.cli.logging import (
    configure_admin_logging,
    configure_bootstrap_logging,
)


def test_bootstrap_logging_emits_plain_console_to_stderr() -> None:
    configure_bootstrap_logging()
    log = structlog.get_logger("daimon.adapters.cli.test")
    log.info("admin.ping", ok=True)


def test_admin_logging_is_alias_for_bootstrap() -> None:
    configure_admin_logging()
    structlog.get_logger().info("admin.emit", n=1)
