"""Entry point: ``python -m compute_session``."""

from __future__ import annotations

import os
import pwd
import sys

import uvicorn

from compute_session.server import create_app

#: Owns the server process, so that commands cannot signal or inspect it.
SERVER_USER = "server"
#: Runs the commands and owns the files of the session.
WORK_USER = "work"


def separate_users() -> None:
    """Give the server one user to be and another to act as. Needs a start as root.

    Who may signal or inspect a process is decided by its real and saved user. Whose
    files it reads and writes is decided by its effective user. The server takes
    ``SERVER_USER`` as the first and ``WORK_USER`` as the second: its file tools act
    as the session owner, a command it starts drops to ``WORK_USER`` for good
    (``tools.run_bash``), and from there the server and process 1 are out of reach.
    """
    if os.geteuid() != 0:
        print(
            "compute_session: not started as root, so commands run as the server's own "
            "user and can stop the server.",
            file=sys.stderr,
        )
        return
    try:
        server, work = pwd.getpwnam(SERVER_USER), pwd.getpwnam(WORK_USER)
    except KeyError:
        sys.exit(f"Users {SERVER_USER!r} and {WORK_USER!r} must exist to start as root.")
    # Groups first: changing them needs the root user this process is about to give up.
    os.setgroups([])
    os.setresgid(server.pw_gid, work.pw_gid, server.pw_gid)
    os.setresuid(server.pw_uid, work.pw_uid, server.pw_uid)


def main() -> None:
    # Taken out of the environment so that the commands this server starts do not
    # inherit it.
    token = os.environ.pop("MCP_AUTH_TOKEN", "")
    if not token.strip():
        sys.exit("MCP_AUTH_TOKEN is not set. The server does not start without it.")
    separate_users()
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
