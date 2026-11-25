"""Office parsing in a child process, so a hostile file can only exhaust the child.

Its address space is limited (RLIMIT_AS, set in `office_child.py`) and the parent kills it at
a wall-clock limit. A failed, killed or over-limit parse has no text. A fresh interpreter, not
a fork: a fork from a worker thread shares the thread's preset memory arena, which RLIMIT_AS
cannot see.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path
from typing import Final

CHILD: Final = Path(__file__).with_name("office_child.py")
MEMORY: Final = 256 << 20
"""Bytes the child may grow past its start (a part is bounded to about 10 MB; a file to 20 MB)."""
TIMEOUT: Final = 20.0
"""Seconds a file may take, start included."""


def _run(mime: str, blob: bytes) -> str | None:
    try:
        done = subprocess.run(
            [sys.executable, "-I", "-S", str(CHILD), mime, str(MEMORY)],
            input=blob,
            capture_output=True,
            timeout=TIMEOUT,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError):  # run() kills and reaps the child on a timeout
        return None
    return done.stdout.decode("utf-8", errors="replace") if done.returncode == 0 else None


async def office_text(mime: str, blob: bytes) -> str | None:
    """The words of a docx, xlsx or pptx read in a child; None when the parse failed, was
    refused, ran out of memory or time."""
    return await asyncio.to_thread(_run, mime, blob)
