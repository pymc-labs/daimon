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


def configure_log_level(level: str) -> None:
    processors: list[structlog.typing.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.dict_tracebacks,
        structlog.processors.JSONRenderer(),
    ]
    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(logging.getLevelNamesMapping()[level]),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=False,
    )
