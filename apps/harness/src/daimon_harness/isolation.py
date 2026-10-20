"""Keep startup credentials and session files inaccessible to tool processes."""

from __future__ import annotations

import ctypes
import os
import sys
from pathlib import Path


def protect_process() -> None:
    if sys.platform != "linux":
        raise RuntimeError("the harness requires Linux process isolation")
    library = ctypes.CDLL(None, use_errno=True)
    if library.prctl(4, 0, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "cannot protect harness process")
    for name in tuple(os.environ):
        if (
            name in {"IDENTITY_HEADER", "MSI_ENDPOINT", "IDENTITY_ENDPOINT"}
            or name.startswith("HARNESS_MCP_CRED_")
            or any(marker in name.upper() for marker in ("TOKEN", "SECRET", "PASSWORD", "API_KEY"))
        ):
            os.environ.pop(name, None)


def tool_identity(home: Path, uid: int = 10001) -> tuple[int | None, int | None, list[int] | None]:
    home.mkdir(parents=True, exist_ok=True)
    work = home / "work"
    work.mkdir(exist_ok=True)
    if os.geteuid() != 0:
        return None, None, None
    # Each session has a persistent numeric identity; no account database mutation.
    # Only that identity can traverse the service-owned session directory.
    os.chown(home, 0, uid)
    os.chmod(home, 0o710)
    os.chown(work, uid, uid)
    os.chmod(work, 0o700)
    return uid, uid, []
