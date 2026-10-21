"""Run one process per persistent session sandbox."""

import os

import uvicorn

if __name__ == "__main__":
    uvicorn.run(
        (
            "daimon_harness.controller:create_app"
            if os.environ.get("HARNESS_ROLE") == "controller"
            else "daimon_harness.service:create_app"
        ),
        factory=True,
        loop="asyncio",
        host="0.0.0.0",
        port=8088,
        timeout_graceful_shutdown=10,
    )
