"""uvicorn entrypoint: `uv run python -m report_host`."""

from __future__ import annotations

import copy
from typing import Any

import uvicorn
from uvicorn.config import LOGGING_CONFIG

from report_host.config import load_settings
from report_host.main import create_app


def uvicorn_log_config() -> dict[str, Any]:
    """uvicorn's default logging, with capability tokens redacted from every record."""
    config: dict[str, Any] = copy.deepcopy(LOGGING_CONFIG)
    config.setdefault("filters", {})["redact_request_targets"] = {
        "()": "report_host.logs.RedactRequestTargets"
    }
    for handler in config["handlers"].values():
        handler.setdefault("filters", []).append("redact_request_targets")
    config["root"] = {"handlers": ["default"], "level": "INFO"}
    # httpx logs every outbound URL at INFO.
    for name in ("httpx", "httpcore"):
        config["loggers"][name] = {"level": "WARNING"}
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
