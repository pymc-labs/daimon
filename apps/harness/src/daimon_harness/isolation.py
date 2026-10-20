"""Keep startup credentials and session files inaccessible to tool processes."""

from __future__ import annotations

import ctypes
import os
import stat
import sys
from pathlib import Path
from typing import Literal

TOOL_UID_MIN = 10001
TOOL_UID_MAX = 60000


def proc_hides_other_users(mountinfo: Path = Path("/proc/self/mountinfo")) -> bool:
    """Inspect the actual proc mount, including its superblock options."""
    for line in mountinfo.read_text().splitlines():
        mount, separator, filesystem = line.partition(" - ")
        fields, backing = mount.split(), filesystem.split()
        if separator and fields[4] == "/proc" and backing[0] == "proc":
            options = set(fields[5].split(",")) | set(backing[2].split(","))
            return bool(options & {"hidepid=2", "hidepid=invisible"}) and not any(
                option.startswith("gid=") for option in options
            )
    return False


def validate_session_mode(
    *, mountinfo: Path = Path("/proc/self/mountinfo"), shared_tmp: Path = Path("/tmp")
) -> Literal["single", "multi"]:
    mode = os.environ.get("HARNESS_SESSION_MODE", "single")
    if mode not in {"single", "multi"}:
        raise RuntimeError("HARNESS_SESSION_MODE must be single or multi")
    if mode == "multi" and os.environ.get("HARNESS_TEST_MODE") != "1":
        if not can_switch_uid() or not proc_hides_other_users(mountinfo):
            raise RuntimeError("multi session mode requires UID switching and hidepid=2 /proc")
        info = shared_tmp.stat()
        # Refuse writable global scratch, including ACL grants that mode bits hide.
        if (
            info.st_mode & stat.S_IWOTH
            or TOOL_UID_MIN <= info.st_uid <= TOOL_UID_MAX
            or (info.st_mode & stat.S_IWGRP and TOOL_UID_MIN <= info.st_gid <= TOOL_UID_MAX)
            or "system.posix_acl_access" in os.listxattr(shared_tmp)
        ):
            raise RuntimeError("multi session mode requires /tmp not writable by tool UIDs")
    return "single" if mode == "single" else "multi"


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
    directories = [home / name for name in ("work", "tmp")]
    for directory in directories:
        if directory.is_symlink():
            raise RuntimeError("tool directories must not be symlinks")
        directory.mkdir(exist_ok=True, mode=0o700)
    if not can_switch_uid():
        if os.environ.get("HARNESS_TEST_MODE") != "1":
            raise RuntimeError("tool execution requires a separate UID")
        return None, None, None
    # Each session has a persistent numeric identity; no account database mutation.
    # Only that identity can traverse the service-owned session directory.
    os.chown(home, os.geteuid(), uid)
    os.chmod(home, 0o710)
    for directory in directories:
        if directory.stat().st_uid != uid:
            os.chmod(directory, 0o700)
            os.chown(directory, uid, uid)
    return uid, uid, []


class CapabilityHeader(ctypes.Structure):
    _fields_ = [("version", ctypes.c_uint32), ("pid", ctypes.c_int)]


class CapabilityData(ctypes.Structure):
    _fields_ = [
        ("effective", ctypes.c_uint32),
        ("permitted", ctypes.c_uint32),
        ("inheritable", ctypes.c_uint32),
    ]


# The service needs identity switching and ownership setup, but no root filesystem bypass.
SERVICE_CAPABILITIES = sum(1 << capability for capability in (0, 5, 6, 7))


def can_switch_uid() -> bool:
    if os.geteuid() == 0:
        return True
    if sys.platform != "linux":
        return False
    header = CapabilityHeader(0x20080522, 0)
    data = (CapabilityData * 2)()
    library = ctypes.CDLL(None, use_errno=True)
    if library.capget(ctypes.byref(header), data) != 0:
        return False
    return data[0].effective & SERVICE_CAPABILITIES == SERVICE_CAPABILITIES


def prepare_service(home: Path) -> None:
    if not can_switch_uid():
        raise RuntimeError("production requires separate service and tool UIDs")
    service_uid = 10000
    if os.geteuid() == service_uid:
        return
    home.mkdir(parents=True, exist_ok=True)
    os.chown(home, service_uid, service_uid)
    home.chmod(0o711)
    journal = home / ".daimon"
    if journal.is_symlink():
        raise RuntimeError("journal root must not be a symlink")
    for path in (journal, *journal.rglob("*")):
        if path.is_symlink():
            raise RuntimeError("journal must not contain symlinks")
        os.chown(path, service_uid, service_uid)
        path.chmod(0o700 if path.is_dir() else 0o600)
    sessions = home / "sessions"
    sessions.mkdir(exist_ok=True)
    os.chown(sessions, service_uid, service_uid)
    sessions.chmod(0o711)
    for sandbox in sessions.iterdir():
        if sandbox.is_symlink() or not sandbox.is_dir():
            raise RuntimeError("invalid session directory")
        os.chown(sandbox, service_uid, sandbox.stat().st_gid)
        sandbox.chmod(0o710)
        skills = sandbox / "skills"
        if skills.exists():
            for path in (skills, *skills.rglob("*")):
                if path.is_symlink():
                    raise RuntimeError("skills must not contain symlinks")
                os.chown(path, service_uid, service_uid)
    library = ctypes.CDLL(None, use_errno=True)
    if library.prctl(8, 1, 0, 0, 0) != 0:  # PR_SET_KEEPCAPS
        raise OSError(ctypes.get_errno(), "cannot retain service identity capabilities")
    os.setgroups([])
    os.setresgid(service_uid, service_uid, service_uid)
    os.setresuid(service_uid, service_uid, service_uid)
    header = CapabilityHeader(0x20080522, 0)
    data = (CapabilityData * 2)()
    data[0].effective = data[0].permitted = SERVICE_CAPABILITIES
    if library.capset(ctypes.byref(header), data) != 0:
        raise OSError(ctypes.get_errno(), "cannot configure service identity capabilities")
    if not can_switch_uid():
        raise RuntimeError("service identity switching unavailable")
