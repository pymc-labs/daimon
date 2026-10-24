"""``python -m mux.testing.harness_conformance --harness-url URL --token TOKEN``.

Serves the fake model on loopback and runs every case that needs no restart hook. Configure the
harness under test to call the printed fake-model URL as its model endpoint before running.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from ..fake_model_server.runner import serve
from .runner import format_report, run_conformance
from .target import HarnessTarget


async def _main(harness_url: str, token: str, fake_port: int, only: list[str] | None) -> int:
    async with serve(port=fake_port) as (model_url, fake):
        print(f"fake model listening at {model_url}", flush=True)
        target = HarnessTarget(base_url=harness_url.rstrip("/"), token=token, model=fake)
        results = await run_conformance(target, only=only)
    print(format_report(results))
    return 1 if any(result.status == "fail" for result in results) else 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--harness-url", required=True)
    parser.add_argument("--token", required=True)
    parser.add_argument("--fake-port", type=int, default=0, help="loopback port for the fake model")
    parser.add_argument("--case", action="append", dest="cases", help="run only this case")
    args = parser.parse_args()
    sys.exit(asyncio.run(_main(args.harness_url, args.token, args.fake_port, args.cases)))


if __name__ == "__main__":
    main()
