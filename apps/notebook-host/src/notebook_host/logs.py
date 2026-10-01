"""Logging filters for the host process."""

from __future__ import annotations

import logging
import re

from uvicorn.logging import AccessFormatter, DefaultFormatter

# Plain, and url-encoded once or twice (marimo's login redirect puts the
# original path, query included, into ``next=``). Tokens are url-safe
# base64, so a value ends at the first ``&``, ``%``, quote or space.
_ACCESS_TOKEN = re.compile(r"(access_token(?:=|%3D|%253D))[^&%\s\"']*", re.IGNORECASE)


def redact_access_token(text: str) -> str:
    return _ACCESS_TOKEN.sub(r"\1[redacted]", text)


class RedactingDefaultFormatter(DefaultFormatter):
    """uvicorn's formatter, redacting the final text, tracebacks included."""

    def format(self, record: logging.LogRecord) -> str:
        return redact_access_token(super().format(record))


class RedactingAccessFormatter(AccessFormatter):
    """uvicorn's access formatter, redacting the final text."""

    def format(self, record: logging.LogRecord) -> str:
        return redact_access_token(super().format(record))


class RedactAccessToken(logging.Filter):
    """Blank ``access_token=`` in log records.

    A notebook link carries its marimo token in the query string, so the first
    request for every link would otherwise write it into uvicorn's access log.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        # Arg-wise first: uvicorn's AccessFormatter reads ``record.args`` by
        # position, so its records must keep their args.
        if isinstance(record.args, tuple):
            record.args = tuple(
                redact_access_token(arg) if isinstance(arg, str) else arg for arg in record.args
            )
        if isinstance(record.msg, str):
            record.msg = redact_access_token(record.msg)
        # Anything still leaking came from a non-str arg (httpx logs an
        # ``httpx.URL``): redact the formatted message and drop the args.
        try:
            message = record.getMessage()
        except (TypeError, ValueError):
            return True
        redacted = redact_access_token(message)
        if redacted != message:
            record.msg = redacted
            record.args = None
        return True
