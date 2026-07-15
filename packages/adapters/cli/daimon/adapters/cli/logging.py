"""Two structlog configurations: bootstrap (stderr plain), admin (alias for
bootstrap). Swapped at phase boundaries; never set at import time."""

from __future__ import annotations

import logging
import sys

import structlog


def _base_processors() -> list[structlog.typing.Processor]:
    return [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
    ]


def configure_bootstrap_logging() -> None:
    structlog.configure(
        processors=[*_base_processors(), structlog.dev.ConsoleRenderer(colors=True)],
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        cache_logger_on_first_use=False,
    )


def configure_admin_logging() -> None:
    configure_bootstrap_logging()
