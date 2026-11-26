"""Session output files: the agent saves them, Daimon lists, reads and deletes them."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, cast

from fastapi import HTTPException
from mux.drivers.selfhosted.wire import (
    OUTPUT_DIRECTORY,
    OUTPUT_FILE_MAX_BYTES,
    OUTPUT_FILES_MAX,
    OutputContent,
    OutputFile,
    OutputsResponse,
    output_id,
)
from pydantic import JsonValue

from .isolation import tool_identity

Operation = Literal["list", "read", "delete"]
STATUS = {"not_found": 404, "changed": 409, "too_large": 413}


def outputs_note(work: Path) -> str:
    """Map the shared output guidance onto this harness's directories."""
    outputs = work / OUTPUT_DIRECTORY
    return (
        f"On this computer, /mnt/session/outputs is {outputs} and /root/work is {work}. "
        f"Files you save in {outputs} are attached to your reply; "
        f"file tools take paths relative to {work}, such as {OUTPUT_DIRECTORY}/report.pdf."
    )


def _run(home: Path, uid: int, payload: dict[str, JsonValue]) -> dict[str, JsonValue]:
    user, group, groups = tool_identity(home, uid)
    result = subprocess.run(
        [sys.executable, "-I", str(Path(__file__).with_name("output_exec.py")), str(home)],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        timeout=30,
        user=user,
        group=group,
        extra_groups=groups,
        cwd=home,
        env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"},
        check=False,
    )
    try:
        reply = cast(dict[str, JsonValue], json.loads(result.stdout))
    except ValueError:
        raise HTTPException(status_code=500, detail="Output operation failed") from None
    error = reply.get("error")
    if isinstance(error, str):
        raise HTTPException(status_code=STATUS.get(error, 422), detail=f"output_{error}")
    return cast(dict[str, JsonValue], reply["result"])


async def operate(
    home: Path, uid: int, operation: Operation, path: str = "", version: str = ""
) -> dict[str, JsonValue]:
    limit = OUTPUT_FILES_MAX if operation == "list" else OUTPUT_FILE_MAX_BYTES
    payload: dict[str, JsonValue] = {
        "operation": operation,
        "path": path,
        "version": version,
        "limit": limit,
    }
    return await asyncio.to_thread(_run, home, uid, payload)


def listing(result: dict[str, JsonValue]) -> OutputsResponse:
    files = cast(list[dict[str, JsonValue]], result["outputs"])
    return OutputsResponse(
        outputs=[
            OutputFile(
                id=output_id(cast(str, item["path"]), cast(str, item["version"])),
                path=cast(str, item["path"]),
                size_bytes=cast(int, item["size_bytes"]),
                modified_at=datetime.fromtimestamp(cast(int, item["modified_ns"]) / 1e9, UTC),
            )
            for item in files
        ],
        truncated=bool(result["truncated"]),
    )


def content(identifier: str, result: dict[str, JsonValue]) -> OutputContent:
    return OutputContent(
        id=identifier,
        size_bytes=cast(int, result["size_bytes"]),
        content_b64=cast(str, result["content_b64"]),
    )
