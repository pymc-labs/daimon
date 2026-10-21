"""Run as the tool UID; verify termination even when procfs hides descendants."""

import os
import signal
import time
from pathlib import Path


def terminated(pid: int) -> bool:
    try:
        # A zombie is dead and cannot finish a pending syscall. Its parent
        # (or tini) owns reaping; signal 0 alone cannot distinguish it.
        state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
        return state in {"Z", "X"}
    except FileNotFoundError:
        # hidepid can return ENOENT for a live, non-dumpable process. Only
        # ESRCH from the kernel signal probe proves it disappeared.
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        except PermissionError:
            pass
        return False
    except (OSError, IndexError):
        return False  # Hidden/non-dumpable live tasks are never dismissed.


def kill_uid(timeout: float = 0.2) -> int:
    deadline = time.monotonic() + timeout
    own_pid = os.getpid()
    while True:
        try:
            os.kill(-1, signal.SIGKILL)
        except ProcessLookupError:
            return 0
        # kill(-1, 0) can return success with only inaccessible UIDs present.
        # Probe individual namespace PIDs instead. Unlike /proc enumeration,
        # this also finds our non-dumpable descendants under hidepid=2.
        try:
            high = int(Path("/proc/sys/kernel/ns_last_pid").read_text())
        except (OSError, ValueError):
            return 1
        remaining = False
        for pid in range(2, high + 1):
            if pid == own_pid:
                continue
            try:
                os.kill(pid, 0)
            except (ProcessLookupError, PermissionError):
                pass
            else:
                remaining = remaining or not terminated(pid)
            if pid % 256 == 0 and time.monotonic() >= deadline:
                return 1  # Incomplete verification is not success.
        if not remaining:
            return 0
        if time.monotonic() >= deadline:
            return 1
        time.sleep(0.01)


if __name__ == "__main__":
    raise SystemExit(kill_uid())
