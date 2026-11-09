"""Remove inherited tool processes before recovery can publish a terminal."""

import subprocess
import sys
import time
from pathlib import Path

# Each UID's helper has the runtime cleanup budget. Helpers run in parallel so
# the caller's fixed startup budget holds for any number of orphan UIDs.
HELPER_TIMEOUT = 4.0


def orphan_uids() -> set[int]:
    uids: set[int] = set()
    for entry in Path("/proc").iterdir():
        if not entry.name.isdecimal():
            continue
        try:
            status = (entry / "status").read_text()
        except (FileNotFoundError, ProcessLookupError):
            # The process exited while it was being read.
            continue
        for line in status.splitlines():
            if line.startswith("Uid:"):
                uid = int(line.split()[1])
                if 10001 <= uid <= 60000:
                    uids.add(uid)
    return uids


def sweep() -> None:
    helper = str(Path(__file__).with_name("kill_uid.py"))
    helpers = [
        subprocess.Popen([sys.executable, "-I", helper, str(uid)], env={})
        for uid in sorted(orphan_uids())
    ]
    deadline = time.monotonic() + HELPER_TIMEOUT
    failed = False
    for process in helpers:
        try:
            code = process.wait(timeout=max(0.0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
            failed = True
        else:
            failed = failed or code != 0
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    sweep()
