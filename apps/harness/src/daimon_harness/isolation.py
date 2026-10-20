"""Keep startup credentials and session files inaccessible to tool processes."""

from __future__ import annotations

import ctypes
import os
import re
import stat
import sys
from pathlib import Path
from typing import Literal

TOOL_UID_MIN = 10001
TOOL_UID_MAX = 60000


def validate_credential_sources() -> None:
    """File-backed identity is unsupported even when a model API key is set."""
    for projected in (
        "/var/run/secrets/azure/tokens/azure-identity-token",
        "/var/run/secrets/kubernetes.io/serviceaccount/token",
    ):
        if Path(projected).exists():
            raise RuntimeError("mounted workload identity tokens are unsupported")
    for name, value in os.environ.items():
        upper = name.upper()
        if value and (
            name
            in {
                "AZURE_FEDERATED_TOKEN_FILE",
                "AZURE_CLIENT_CERTIFICATE_PATH",
                "KUBECONFIG",
                "AZURE_CONFIG_DIR",
                "GOOGLE_APPLICATION_CREDENTIALS",
                "AWS_CONFIG_FILE",
            }
            or (
                any(
                    marker in upper
                    for marker in ("TOKEN", "SECRET", "CREDENTIAL", "CERTIFICATE", "PRIVATE_KEY")
                )
                and upper.endswith(("_FILE", "_PATH", "_DIR"))
            )
        ):
            raise RuntimeError(f"file-based credentials are unsupported: {name}")


def credential_mount_roots(home: Path, mountinfo: Path) -> tuple[Path, ...]:
    """Inspect every external mount, independent of its name or read-only flag."""
    roots: list[Path] = []
    for line in mountinfo.read_text().splitlines():
        mount, separator, filesystem = line.partition(" - ")
        fields, backing = mount.split(), filesystem.split()
        if not separator or len(fields) < 6 or len(backing) < 3:
            raise RuntimeError("cannot verify mount isolation")
        path = Path(re.sub(r"\\([0-7]{3})", lambda m: chr(int(m[1], 8)), fields[4]))
        if path == Path("/"):
            continue  # The image supplies the interpreter and tool binaries.
        if path in {
            Path("/etc/hosts"),
            Path("/etc/hostname"),
            Path("/etc/resolv.conf"),
            Path("/sbin/docker-init"),
            Path("/usr/sbin/docker-init"),
        }:
            continue  # Container-runtime network configuration and PID 1 helper.
        if backing[0] in {"proc", "sysfs", "cgroup", "cgroup2", "devpts", "mqueue"} and (
            path.is_relative_to("/proc")
            or path.is_relative_to("/sys")
            or path.is_relative_to("/dev")
        ):
            continue
        if backing[0] == "tmpfs" and (
            path in {Path("/dev"), Path("/dev/shm")}
            or path.is_relative_to("/proc")
            or path.is_relative_to("/sys")
        ):
            continue
        if path.is_relative_to(home / "sessions"):
            continue  # Per-session ownership is verified/prepared before tools run.
        roots.append(path)
    return tuple(roots)


def validate_credential_files(
    roots: tuple[Path, ...] | None = None,
    *,
    home: Path | None = None,
    mountinfo: Path = Path("/proc/self/mountinfo"),
) -> None:
    """Refuse tool-readable regular files on external mounts and in credential caches."""
    session_root = (home / "sessions") if home is not None else None
    if roots is None:
        candidates = {
            Path("/run/secrets"),
            Path("/var/run/secrets"),
            Path("/etc/secrets"),
            Path("/mnt/secrets"),
            Path("/var/secrets"),
        }
        for credential_home in {Path.home(), Path("/root"), Path("/home/harness")}:
            candidates.update(
                credential_home / name
                for name in (".azure", ".aws", ".kube", ".ssh", ".docker", ".config/gcloud")
            )
        candidates.update(credential_mount_roots(home or Path.home(), mountinfo))
        roots = tuple(candidates)

    def scan_error(error: OSError) -> None:
        raise RuntimeError("cannot verify credential file isolation") from error

    for root in roots:
        try:
            info = root.stat()
        except FileNotFoundError:
            continue
        paths = [root]
        if stat.S_ISDIR(info.st_mode):
            seen: set[tuple[int, int]] = set()
            for directory, children, files in os.walk(root, followlinks=True, onerror=scan_error):
                if session_root is not None:
                    children[:] = [
                        name for name in children if Path(directory) / name != session_root
                    ]
                info = Path(directory).stat()
                identity = (info.st_dev, info.st_ino)
                if identity in seen:
                    children.clear()
                    continue
                seen.add(identity)
                paths.extend(Path(directory) / name for name in files)
        for path in paths:
            if not path.is_file():
                continue
            # Refuse ambiguous ACL grants rather than relying only on mode bits.
            chain = (path, *path.parents, path.resolve(), *path.resolve().parents)
            if any("system.posix_acl_access" in os.listxattr(part) for part in chain):
                raise RuntimeError("credential file access cannot be verified")
            identities = {TOOL_UID_MIN}
            for part in chain:
                info = part.stat()
                identities.update(
                    uid for uid in (info.st_uid, info.st_gid) if TOOL_UID_MIN <= uid <= TOOL_UID_MAX
                )
            for uid in identities:

                def permitted(part: Path, uid: int = uid) -> bool:
                    info = part.stat()
                    shift = 6 if info.st_uid == uid else 3 if info.st_gid == uid else 0
                    return bool(info.st_mode & ((4 if part.is_file() else 1) << shift))

                if all(permitted(part) for part in chain):
                    raise RuntimeError("credential/secret files must not be readable by tool UIDs")


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
    *, mountinfo: Path = Path("/proc/self/mountinfo"), shared_roots: tuple[Path, ...] | None = None
) -> Literal["single", "multi"]:
    mode = os.environ.get("HARNESS_SESSION_MODE", "single")
    if mode not in {"single", "multi"}:
        raise RuntimeError("HARNESS_SESSION_MODE must be single or multi")
    if mode == "multi" and os.environ.get("HARNESS_TEST_MODE") != "1":
        if not can_switch_uid() or not proc_hides_other_users(mountinfo):
            raise RuntimeError("multi session mode requires UID switching and hidepid=2 /proc")
        validate_shared_directories(
            shared_storage_roots(mountinfo) if shared_roots is None else shared_roots
        )
    return "single" if mode == "single" else "multi"


def protect_process() -> None:
    if sys.platform != "linux":
        raise RuntimeError("the harness requires Linux process isolation")
    library = ctypes.CDLL(None, use_errno=True)
    if library.prctl(4, 0, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "cannot protect harness process")
    for name in tuple(os.environ):
        if (
            name in {"IDENTITY_HEADER", "MSI_ENDPOINT", "IDENTITY_ENDPOINT", "AZURE_CLIENT_ID"}
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


def _scan_error(error: OSError) -> None:
    raise RuntimeError("cannot verify shared storage isolation") from error


def shared_storage_roots(mountinfo: Path) -> tuple[Path, ...]:
    roots: list[Path] = []
    for line in mountinfo.read_text().splitlines():
        mount, _, filesystem = line.partition(" - ")
        fields, backing = mount.split(), filesystem.split()
        if "rw" in fields[5].split(",") and backing[0] not in {
            "proc",
            "sysfs",
            "cgroup",
            "cgroup2",
            "devpts",
            "mqueue",
        }:
            path = Path(fields[4].replace(r"\040", " ").replace(r"\134", "\\"))
            if path.is_dir():
                roots.append(path)
    return tuple(roots)


def validate_shared_directories(roots: tuple[Path, ...]) -> None:
    """Refuse shared writable storage, including mounted volumes and ACLs."""
    seen: set[tuple[int, int]] = set()
    for root in roots:
        for directory, children, _ in os.walk(root, followlinks=False, onerror=_scan_error):
            path = Path(directory)
            # Mounts are scanned separately; never descend into proc/sysfs.
            children[:] = [name for name in children if not (path / name).is_mount()]
            info = path.stat()
            identity = (info.st_dev, info.st_ino)
            if identity in seen:
                children.clear()
                continue
            seen.add(identity)
            # A service-owned session boundary grants traversal only to its
            # assigned group. Its private writable children are not shared.
            if (
                not TOOL_UID_MIN <= info.st_uid <= TOOL_UID_MAX
                and TOOL_UID_MIN <= info.st_gid <= TOOL_UID_MAX
                and info.st_mode & 0o027 == 0
                and "system.posix_acl_access" not in os.listxattr(path)
            ):
                children.clear()
                continue
            if (
                info.st_mode & stat.S_IWOTH
                or TOOL_UID_MIN <= info.st_uid <= TOOL_UID_MAX
                or (info.st_mode & stat.S_IWGRP and TOOL_UID_MIN <= info.st_gid <= TOOL_UID_MAX)
                or "system.posix_acl_access" in os.listxattr(path)
            ):
                raise RuntimeError(
                    f"multi session mode requires shared directory {path} not writable by tool UIDs"
                )
