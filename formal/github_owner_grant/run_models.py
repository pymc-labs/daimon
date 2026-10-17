"""Check bounded owner-grant outcomes with one TLC worker and capped metadata."""

from __future__ import annotations

import csv
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
JAR = Path(os.environ.get("TLA2TOOLS_JAR", str(Path.home() / ".cache/daimon/tla/tla2tools.jar")))
TIMEOUT_SECONDS = 45
MAX_METADIR_BYTES = 64 * 1024 * 1024


def directory_bytes(path: Path) -> int:
    return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())


def run(config: str) -> tuple[str, int]:
    metadir = Path(tempfile.mkdtemp(prefix="tlc-ownergrant-"))
    process = subprocess.Popen(
        [
            "java",
            "-Xmx512m",
            "-XX:+UseParallelGC",
            "-cp",
            str(JAR),
            "tlc2.TLC",
            "-workers",
            "1",
            "-seed",
            "1",
            "-fp",
            "0",
            "-metadir",
            str(metadir),
            "-config",
            f"{config}.cfg",
            "OwnerGrant.tla",
        ],
        cwd=HERE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    started = time.monotonic()
    try:
        while process.poll() is None:
            if time.monotonic() - started > TIMEOUT_SECONDS:
                raise RuntimeError(f"{config}: TLC exceeded {TIMEOUT_SECONDS}s")
            if directory_bytes(metadir) > MAX_METADIR_BYTES:
                raise RuntimeError(f"{config}: TLC metadir exceeded 64 MiB")
            time.sleep(0.1)
        output = process.communicate()[0]
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate()
        shutil.rmtree(metadir)

    violation = re.search(r"Error: Invariant (\w+) is violated", output)
    if violation and process.returncode == 12:
        verdict = f"violates:{violation.group(1)}"
    elif process.returncode == 0 and "No error has been found" in output:
        verdict = "clean"
    else:
        raise RuntimeError(f"{config}: unexpected TLC result\n{output[-2000:]}")
    counts = re.findall(r"(\d+) states generated, (\d+) distinct states found", output)
    if not counts or int(counts[-1][1]) == 0:
        raise RuntimeError(f"{config}: no nonzero distinct-state count\n{output[-2000:]}")
    return verdict, int(counts[-1][1])


def main() -> int:
    if not JAR.is_file():
        raise RuntimeError(f"TLA+ tools jar missing: {JAR}")
    with (HERE / "expected.tsv").open(newline="") as source:
        rows = list(csv.DictReader(source, delimiter="\t"))
    failed = False
    for row in rows:
        actual = run(row["config"])
        expected = (row["verdict"], int(row["distinct_states"]))
        print(f"{row['config']}: {actual[0]}, {actual[1]} distinct states")
        failed |= actual != expected
        if actual != expected:
            print(f"  expected {expected[0]}, {expected[1]} distinct states")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
