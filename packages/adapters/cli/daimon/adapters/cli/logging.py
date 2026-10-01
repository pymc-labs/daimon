"""Two structlog configurations: bootstrap (stderr plain), admin (alias for
bootstrap). Swapped at phase boundaries; never set at import time."""

from __future__ import annotations

import logging
import sys

import structlog
from daimon.core.observability import redact_log_event


def _base_processors() -> list[structlog.typing.Processor]:
    return [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
    ]


def configure_bootstrap_logging() -> None:
    structlog.configure(
        processors=[
            *_base_processors(),
            # Render tracebacks to text first so the redaction sees them.
            structlog.processors.format_exc_info,
            redact_log_event,
            structlog.dev.ConsoleRenderer(
                colors=True,
                # Tracebacks must not print frame locals: they can hold decrypted keys.
                exception_formatter=structlog.dev.RichTracebackFormatter(show_locals=False),
            ),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        cache_logger_on_first_use=False,
    )


def configure_admin_logging() -> None:
    configure_bootstrap_logging()
