"""The OB-1 production logging chain: structured JSON for discord/scheduler/mcp.

`configure_log_level` installs structlog's full processor chain — every line
renders as JSON carrying the contextvar-bound `rid`/`tenant_id`, a level, an
iso-utc timestamp, and a structured exception array on `log.exception(...)`.
The wrapper's filtering level makes `DAIMON_LOG__LEVEL` effective without a
code change.

Call this explicitly at each process entrypoint, NEVER at import time —
configuring structlog mutates global state, and import-time side effects make
the render target depend on import order. The CLI keeps its own
`ConsoleRenderer` configuration (`adapters/cli/logging.py`); this module is the
non-CLI render target and intentionally does not share it.
"""

from __future__ import annotations

import logging

import structlog
from daimon.core.observability import install_log_redaction, redact_log_event, redact_rendered


def configure_log_level(level: str) -> None:
    processors: list[structlog.typing.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.ExceptionRenderer(
            structlog.tracebacks.ExceptionDictTransformer(show_locals=False)
        ),
        # Exception text and string fields can quote a credential.
        redact_log_event,
        structlog.processors.JSONRenderer(),
        # Last: redact the rendered line itself.
        redact_rendered,
    ]
    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(logging.getLevelNamesMapping()[level]),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=False,
    )


def configure_logging(level: str) -> None:
    """The one logging setup for every service entrypoint: the redacting JSON
    structlog chain, plus redaction of all stdlib log output in the process
    (every handler including `logging.lastResort`, a root stderr handler,
    warnings and unraisable exceptions). Call before the first log line."""
    configure_log_level(level)
    install_log_redaction()
