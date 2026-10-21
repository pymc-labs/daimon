"""Signal as the tool identity and verify every present PID through trusted procfs."""

import ctypes
import os
import signal
import sys
import time
from contextlib import suppress
from pathlib import Path

# Only the trusted cleanup helper receives this procfs exemption group.
PROC_VERIFY_GID = 60002
# A distinct verifier identity per tool UID also isolates concurrent sessions.
VERIFY_UID_BASE = 100_000


def terminated(pid: int) -> bool:
    try:
        # A zombie is dead and cannot finish a pending syscall. Its parent
        # (or tini) owns reaping; signal 0 alone cannot distinguish it.
        state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
        if state not in {"Z", "X"}:
            return False
        # pthread_exit can leave the leader zombie while another thread is
        # still executing. Inspect every present thread before declaring death.
        for thread in Path(f"/proc/{pid}/task").iterdir():
            try:
                if (thread / "stat").read_text().rsplit(")", 1)[1].split()[0] not in {"Z", "X"}:
                    return False
            except FileNotFoundError:
                continue
        return True
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


def visible_proc(mountinfo: Path = Path("/proc/self/mountinfo")) -> bool:
    """Enumeration is proof only if procfs cannot hide non-dumpable tool tasks."""
    for line in reversed(mountinfo.read_text().splitlines()):
        mount, separator, filesystem = line.partition(" - ")
        fields, backing = mount.split(), filesystem.split()
        if separator and fields[4] == "/proc" and backing[0] == "proc":
            options = set(fields[5].split(",")) | set(backing[2].split(","))
            modes = {o.partition("=")[2] for o in options if o.startswith("hidepid=")}
            if not modes or modes == {"0"}:
                return True
            # hidepid=4/ptraceable has different visibility semantics. Do not
            # assume an exemption works for unknown procfs modes.
            if not modes <= {"1", "2", "noaccess", "invisible"}:
                return False
            groups = {*os.getgroups(), os.getegid()}
            return any(f"gid={gid}" in options for gid in groups)
    return False


def present_pids() -> list[int]:
    # readdir is proportional to present processes, independent of ns_last_pid
    # and pid_max wrap. Do not replace this with a PID allocation-range scan.
    return [int(entry.name) for entry in Path("/proc").iterdir() if entry.name.isdecimal()]


def kill_uid(timeout: float = 0.2) -> int:
    deadline = time.monotonic() + timeout
    own_pid = os.getpid()
    try:
        if not visible_proc():
            return 1
        while True:
            with suppress(ProcessLookupError):
                os.kill(-1, signal.SIGKILL)
            remaining = False
            for pid in present_pids():
                if pid in {1, own_pid}:
                    continue
                try:
                    os.kill(pid, 0)
                except (ProcessLookupError, PermissionError):
                    pass
                else:
                    remaining = remaining or not terminated(pid)
                if time.monotonic() >= deadline and remaining:
                    return 1
            if not remaining:
                return 0
            if time.monotonic() >= deadline:
                return 1
            time.sleep(0.01)
    except OSError:
        return 1  # An unreadable proc mount is never proof of termination.


def isolate_verifier(tool_uid: int) -> None:
    """Keep the proc exemption out of reach of tool-UID ptrace attackers."""
    if not 10001 <= tool_uid <= 60000:
        raise ValueError("invalid tool UID")
    os.setgroups([PROC_VERIFY_GID])
    os.setresgid(tool_uid, tool_uid, tool_uid)
    # Signal permission uses real OR effective UID. The real tool UID lets this
    # helper signal that session; distinct effective/saved UIDs forbid ptrace by
    # tools, including during interpreter startup (which runs as root with -I).
    # All root UIDs are dropped, clearing Linux permitted/effective capabilities.
    verify_uid = VERIFY_UID_BASE + tool_uid
    os.setresuid(tool_uid, verify_uid, verify_uid)
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(4, 0, 0, 0, 0) != 0 or libc.prctl(38, 1, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "cannot protect cleanup verifier")


if __name__ == "__main__":
    isolate_verifier(int(sys.argv[1]))
    raise SystemExit(kill_uid())
