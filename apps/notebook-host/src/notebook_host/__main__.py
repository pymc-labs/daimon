"""uvicorn entrypoint: `uv run python -m notebook_host`."""

from __future__ import annotations

import copy
from typing import Any

import uvicorn
from uvicorn.config import LOGGING_CONFIG

from notebook_host.config import load_settings
from notebook_host.main import create_app


def uvicorn_log_config() -> dict[str, Any]:
    """uvicorn's default logging, with notebook tokens redacted from every handler."""
    config: dict[str, Any] = copy.deepcopy(LOGGING_CONFIG)
    config.setdefault("filters", {})["redact_access_token"] = {
        "()": "notebook_host.logs.RedactAccessToken"
    }
    for handler in config["handlers"].values():
        handler.setdefault("filters", []).append("redact_access_token")
    return config


def main() -> None:
    settings = load_settings()
    app = create_app(settings)
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=settings.host_port,
        log_level="info",
        log_config=uvicorn_log_config(),
    )


if __name__ == "__main__":
    main()
