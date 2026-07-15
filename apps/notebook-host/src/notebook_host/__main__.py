"""uvicorn entrypoint: `uv run python -m notebook_host`."""

from __future__ import annotations

import uvicorn

from notebook_host.config import load_settings
from notebook_host.main import create_app


def main() -> None:
    settings = load_settings()
    app = create_app(settings)
    uvicorn.run(app, host="0.0.0.0", port=settings.host_port, log_level="info")


if __name__ == "__main__":
    main()
