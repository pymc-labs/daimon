"""uvicorn entrypoint: `uv run python -m report_host`.

The app factory (`create_app`) and its settings module land in a later plan
in this PR. Until then this entrypoint defines a minimal FastAPI app inline
with only the health route the Caddy/compose probe needs, so the service is
importable and runnable at every point in this PR's history.
"""

from __future__ import annotations

import uvicorn
from fastapi import FastAPI

app = FastAPI()


@app.get("/health")
def health() -> dict[str, str]:
    return {"ok": "true"}


def main() -> None:
    uvicorn.run(app, host="0.0.0.0", port=8002, log_level="info")


if __name__ == "__main__":
    main()
