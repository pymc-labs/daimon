"""Run the bounded reconciliation models with pinned TLC results."""

from __future__ import annotations

import csv
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
JARS = (Path.home() / ".cache/daimon/tla/tla2tools.jar",)
MAX_SECONDS = 180
MAX_META_BYTES = 256 * 1024 * 1024


def size(path: Path) -> int:
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file())


def run(config: str, jar: Path) -> tuple[str, int]:
    metadir = Path(tempfile.mkdtemp(prefix=".tlc-", dir=HERE))
    command = [
        "java",
        "-Xmx768m",
        "-XX:+UseParallelGC",
        "-cp",
        str(jar),
        "tlc2.TLC",
        "-deadlock",
        # Check liveness once, after the full search; the default checks at
        # intervals and makes counts of liveness counterexamples vary by timing.
        "-lncheck",
        "final",
        "-workers",
        "1",
        "-metadir",
        str(metadir),
        "-config",
        f"{config}.cfg",
        "Reconcile.tla",
    ]
    process = None
    try:
        process = subprocess.Popen(
            command,
            cwd=HERE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        start = time.monotonic()
        while process.poll() is None:
            if time.monotonic() - start > MAX_SECONDS or size(metadir) > MAX_META_BYTES:
                process.kill()
                raise RuntimeError(f"{config}: time or metadir cap exceeded")
            time.sleep(0.1)
        output = process.communicate()[0]
        match = re.findall(r"(\d+) states generated, (\d+) distinct states found", output)
        if not match:
            raise RuntimeError(f"{config}: no state count\n{output[-2000:]}")
        distinct = int(match[-1][1])
        if distinct == 0 or "Deadlock reached" in output:
            raise RuntimeError(f"{config}: zero states or unexpected deadlock")
        if "Parsing or semantic analysis failed" in output or "Exception" in output:
            raise RuntimeError(f"{config}: TLC failed\n{output[-2000:]}")
        if "No error has been found" in output:
            verdict = "clean"
        elif "Invariant NoStaleOverwrite is violated" in output:
            verdict = "violates:NoStaleOverwrite"
        elif "Temporal properties were violated" in output:
            # Name the property from the config; only configs with one PROPERTY may violate.
            properties = re.findall(r"^PROPERTY (\w+)$", (HERE / f"{config}.cfg").read_text(), re.M)
            if len(properties) != 1:
                raise RuntimeError(f"{config}: cannot name the violated property")
            verdict = f"violates:{properties[0]}"
        elif "Invariant NoReorderDuplicateWitness is violated" in output:
            verdict = "witness:ReorderDuplicateConverges"
        else:
            raise RuntimeError(f"{config}: unknown TLC result\n{output[-2000:]}")
        return verdict, distinct
    finally:
        if process is not None and process.poll() is None:
            process.kill()
            process.wait()
        shutil.rmtree(metadir)


def main() -> None:
    jar = next((path for path in JARS if path.is_file()), None)
    if jar is None:
        raise SystemExit("tla2tools.jar is missing")
    with (HERE / "expected.tsv").open(newline="") as stream:
        cases = list(csv.DictReader(stream, delimiter="\t"))
    for case in cases:
        verdict, count = run(case["config"], jar)
        expected = (case["verdict"], int(case["distinct_states"]))
        actual = (verdict, count)
        print(f"{case['config']}: {verdict}, {count} distinct states")
        if actual != expected:
            raise SystemExit(f"expected {expected}, got {actual}")


if __name__ == "__main__":
    main()
