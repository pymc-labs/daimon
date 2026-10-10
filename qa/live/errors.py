"""Private traceback evidence without locals or credential values."""

from __future__ import annotations

import os
import re
import traceback

from pydantic import JsonValue

from qa.live.types import Message


def redact(value: str) -> str:
    for name, secret in os.environ.items():
        if len(secret) >= 8 and re.search(r"TOKEN|KEY|SECRET|PASSWORD|DATABASE", name):
            value = value.replace(secret, "[redacted]")
    value = re.sub(r"(://)[^\s/@]+:[^\s/@]+@", r"\1[redacted]@", value)
    return re.sub(
        r"(?i)((?:access_token|api_key|password|authorization)\s*[=:]\s*)[^\s,;]+",
        r"\1[redacted]",
        value,
    )


def exception_evidence(exc: BaseException, stage: str) -> Message:
    frames: list[JsonValue] = []
    for frame in traceback.extract_tb(exc.__traceback__):
        frames.append(
            {
                "file": redact(frame.filename),
                "line": frame.lineno,
                "function": frame.name,
                "source": redact(frame.line) if frame.line else None,
            }
        )
    return {
        "stage": stage,
        "type": type(exc).__name__,
        "message": redact(str(exc)),
        "frames": frames,
        "traceback": redact("".join(traceback.format_exception(exc))),
    }
