"""Entry point: ``python -m compute_session``."""

from __future__ import annotations

import os
import sys

import uvicorn

from compute_session.server import create_app


def main() -> None:
    # Taken out of the environment so the commands this server starts do not
    # inherit it. That keeps the token out of their output; it is not a wall,
    # because they run as the same user as the server.
    token = os.environ.pop("MCP_AUTH_TOKEN", "")
    if not token.strip():
        sys.exit("MCP_AUTH_TOKEN is not set. The server does not start without it.")
    uvicorn.run(
        create_app(token),
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "8080")),
        lifespan="off",
        server_header=False,
        # A stop signal ends the server within a platform's shutdown window even
        # while an inline command is still running.
        timeout_graceful_shutdown=5,
    )


if __name__ == "__main__":
    main()
