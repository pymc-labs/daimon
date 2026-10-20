"""Run one process per persistent session sandbox."""

import uvicorn

if __name__ == "__main__":
    uvicorn.run(
        "daimon_harness.service:create_app",
        factory=True,
        host="0.0.0.0",
        port=8088,
        timeout_graceful_shutdown=10,
    )
