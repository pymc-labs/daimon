"""The production logging chain: one structured JSON line per log record.

`configure_log_level` installs structlog's full processor chain — every line
renders as JSON carrying the contextvar-bound `rid`/`tenant_id`, a level and
its Cloud-style `severity`, an iso-utc timestamp, and a structured exception
array on `log.exception(...)`. The wrapper's filtering level makes
`DAIMON_LOG__LEVEL` effective without a code change.

`configure_logging` also renders stdlib records (discord.py, asyncio, uvicorn,
`_log.exception` in core modules) through the same JSON shape, so a traceback
is one line with a structured `exception`, and logs uncaught exceptions as
`process.uncaught_exception`. `mark_shutting_down` flags every later line with
`shutting_down: true` so a deploy's stop noise can be told from a real fault.

Call this explicitly at each process entrypoint, NEVER at import time —
configuring structlog mutates global state, and import-time side effects make
the render target depend on import order. The CLI keeps its own
`ConsoleRenderer` configuration (`adapters/cli/logging.py`); this module is the
non-CLI render target and intentionally does not share it.
"""

from __future__ import annotations

import logging
import sys
import threading
from types import TracebackType
from typing import TextIO

import structlog
from daimon.core.observability import (
    JsonLineFormatter,
    install_log_redaction,
    redact_log_event,
    redact_rendered,
    set_unraisable_logger,
)

_SEVERITY = {
    "debug": "DEBUG",
    "info": "INFO",
    "warning": "WARNING",
    "error": "ERROR",
    "critical": "CRITICAL",
}
_UVICORN_LOGGERS = ("uvicorn", "uvicorn.error", "uvicorn.access")
_shutting_down = False


def mark_shutting_down() -> None:
    """Flag every later log line `shutting_down: true`; call when SIGTERM or
    SIGINT starts the process's shutdown. Idempotent."""
    global _shutting_down
    _shutting_down = True


def clear_shutting_down() -> None:
    """Undo `mark_shutting_down`; only test isolation needs this."""
    global _shutting_down
    _shutting_down = False


def add_severity(
    logger: structlog.typing.WrappedLogger, method_name: str, event_dict: structlog.typing.EventDict
) -> structlog.typing.EventDict:
    """Set `severity` from `level`; Cloud Run reads it, a VM log matches it as text."""
    del logger, method_name
    event_dict["severity"] = _SEVERITY.get(str(event_dict.get("level")), "DEFAULT")
    return event_dict


def add_shutting_down(
    logger: structlog.typing.WrappedLogger, method_name: str, event_dict: structlog.typing.EventDict
) -> structlog.typing.EventDict:
    del logger, method_name
    if _shutting_down:
        event_dict["shutting_down"] = True
    return event_dict


def _line_stamps() -> list[structlog.typing.Processor]:
    """Level, severity, shutdown flag and timestamp: the fields every line has."""
    return [
        structlog.processors.add_log_level,
        add_severity,
        add_shutting_down,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
    ]


def _render_json() -> list[structlog.typing.Processor]:
    """Exception to structured data, redact, render one line, redact that line."""
    return [
        structlog.processors.ExceptionRenderer(
            structlog.tracebacks.ExceptionDictTransformer(show_locals=False)
        ),
        # Exception text and string fields can quote a credential.
        redact_log_event,
        structlog.processors.JSONRenderer(),
        # Last: redact the rendered line itself.
        redact_rendered,
    ]


def configure_log_level(level: str) -> None:
    processors: list[structlog.typing.Processor] = [
        structlog.contextvars.merge_contextvars,
        *_line_stamps(),
        *_render_json(),
    ]
    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(logging.getLevelNamesMapping()[level]),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=False,
    )


def json_line_formatter() -> JsonLineFormatter:
    """Renders a stdlib record as the same one-line JSON a structlog event gets."""
    return JsonLineFormatter(
        foreign_pre_chain=[structlog.stdlib.add_logger_name, *_line_stamps()],
        processors=[JsonLineFormatter.remove_processors_meta, *_render_json()],
    )


def _log_uncaught(exc: BaseException) -> None:
    structlog.get_logger().critical("process.uncaught_exception", exc_info=exc)


def _excepthook(
    exc_type: type[BaseException], exc: BaseException, tb: TracebackType | None
) -> None:
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc, tb)
        return
    _log_uncaught(exc.with_traceback(tb))


def _threading_excepthook(args: threading.ExceptHookArgs) -> None:
    if args.exc_value is None or issubclass(args.exc_type, SystemExit):
        return
    _log_uncaught(args.exc_value.with_traceback(args.exc_traceback))


def _log_unraisable(unraisable: sys.UnraisableHookArgs) -> None:
    """One `process.unraisable_exception` line: where it happened (redacted by the
    chain) and the structured exception."""
    where = f"{unraisable.err_msg or 'Exception ignored in'}: {unraisable.object!r}"
    exc_info = (
        (unraisable.exc_type, unraisable.exc_value, unraisable.exc_traceback)
        if unraisable.exc_value is not None
        else None
    )
    structlog.get_logger().error(
        "process.unraisable_exception", where=where[:500], exc_info=exc_info
    )


def install_excepthooks() -> None:
    """Log an uncaught exception, in the main thread or any other, as a critical
    `process.uncaught_exception` line, and an unraisable one (a failing `__del__`,
    a callback in a finalizer) as `process.unraisable_exception` at error. The
    hooks only log: the interpreter still exits the way it would have. Idempotent."""
    sys.excepthook = _excepthook
    threading.excepthook = _threading_excepthook
    set_unraisable_logger(_log_unraisable)


def _use_formatter_on_uvicorn_handlers(formatter: logging.Formatter) -> None:
    """Uvicorn's own handlers print plain text; render them as JSON lines too."""
    for name in _UVICORN_LOGGERS:
        for handler in logging.getLogger(name).handlers:
            handler.setFormatter(formatter)


class _CurrentStderrHandler(logging.StreamHandler):  # pyright: ignore[reportMissingTypeArgument]
    """Writes to whatever `sys.stderr` is at the moment of each record."""

    def __init__(self) -> None:
        logging.Handler.__init__(self)

    @property
    def stream(self) -> TextIO:  # pyright: ignore[reportIncompatibleVariableOverride]
        return sys.stderr


def _use_json_instead_of_other_handlers(formatter: logging.Formatter) -> None:
    """fastmcp prints its records, tracebacks included, through rich handlers on
    a logger that does not propagate; swap any handler that is not already JSON
    for one JSON stderr handler."""
    fastmcp_logger = logging.getLogger("fastmcp")
    others = [h for h in fastmcp_logger.handlers if not isinstance(h.formatter, JsonLineFormatter)]
    if not others:
        return
    for handler in others:
        fastmcp_logger.removeHandler(handler)
    replacement = _CurrentStderrHandler()
    replacement.setFormatter(formatter)
    fastmcp_logger.addHandler(replacement)


def configure_logging(level: str) -> None:
    """The one logging setup for every service entrypoint: the redacting JSON
    structlog chain, redaction of all stdlib log output in the process
    (every handler including `logging.lastResort`, a root stderr handler,
    warnings and unraisable exceptions), that stdlib output (including uvicorn's
    and fastmcp's own handlers) rendered as one JSON line per record, and the
    uncaught-exception hooks. Call before the first log line."""
    configure_log_level(level)
    formatter = json_line_formatter()
    install_log_redaction(formatter=formatter)
    _use_formatter_on_uvicorn_handlers(formatter)
    _use_json_instead_of_other_handlers(formatter)
    install_excepthooks()
