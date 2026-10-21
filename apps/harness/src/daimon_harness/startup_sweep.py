"""Remove inherited tool processes before recovery can publish a terminal."""

import subprocess
import sys
from pathlib import Path


def sweep() -> None:
    uids: set[int] = set()
    for entry in Path("/proc").iterdir():
        if not entry.name.isdecimal():
            continue
        try:
            status = (entry / "status").read_text()
        except FileNotFoundError:
            continue
        for line in status.splitlines():
            if line.startswith("Uid:"):
                uid = int(line.split()[1])
                if 10001 <= uid <= 60000:
                    uids.add(uid)
    for uid in sorted(uids):
        subprocess.run(
            [sys.executable, "-I", str(Path(__file__).with_name("kill_uid.py")), str(uid)],
            check=True,
            timeout=2,
            env={},
        )


if __name__ == "__main__":
    sweep()
