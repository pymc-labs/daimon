"""Logging filters for the report host process.

Report links carry the reader's capability in `?k=`, and the upload and
publish routes carry theirs in the path (`/upload/{turn_token}`,
`/publish/{capability_token}`, `/admin/reports/{slug}/recipients/{token}`).
uvicorn's access log would otherwise print every one of them.
"""

from __future__ import annotations

import logging
import re
import traceback

_REDACTED = "[redacted]"
_CAPABILITY_PATH = re.compile(r"(/(?:upload|publish|recipients)/)[^/?#\s\"']+")
_QUERY_VALUE = re.compile(r"([?&][^=&#\s\"']*=)[^&#\s\"']*")
_ACCESS_LOGGER = "uvicorn.access"


def redact_request_text(text: str) -> str:
    """Capability path segments and every query value replaced."""
    text = _CAPABILITY_PATH.sub(r"\1" + _REDACTED, text)
    return _QUERY_VALUE.sub(r"\1" + _REDACTED, text)


class RedactRequestTargets(logging.Filter):
    """Redact request targets in uvicorn access records and any other record."""

    def filter(self, record: logging.LogRecord) -> bool:
        if (
            record.name == _ACCESS_LOGGER
            and isinstance(record.args, tuple)
            and len(record.args) >= 3
        ):
            # uvicorn's AccessFormatter reads ``record.args`` by position.
            args = list(record.args)
            if isinstance(args[2], str):
                args[2] = redact_request_text(args[2])
            record.args = tuple(args)
            return True
        if record.exc_info and not record.exc_text:
            record.exc_text = redact_request_text(
                "".join(traceback.format_exception(*record.exc_info))
            ).rstrip("\n")
        try:
            message = record.getMessage()
        except (TypeError, ValueError):
            return True
        redacted = redact_request_text(message)
        if redacted != message:
            record.msg = redacted
            record.args = None
        return True
