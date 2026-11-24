# Compute session image

The image a [compute session](../../docs/compute.md) machine runs. It holds a
small authenticated MCP server (`compute_session`) and a Python toolchain.
Daimon starts one machine for each session, gives it one token and drives it
with four tools: `run_bash`, `write_file`, `read_file` and `list_files`.

## What the image holds

- The server in its own environment at `/opt/compute-session`, outside `PATH`.
  It depends on Starlette and uvicorn and imports nothing from Daimon.
- Python 3.12, `uv`, `git`, `curl`, `gcc`, `g++` and `make`.
- `bash`, coreutils, util-linux and `tini`, which background commands need.
- A user `work` (uid 10000) with home `/home/work`, who runs the commands, and a
  user `server` (uid 10001), who owns the server process. There is no `sudo`.

The image holds no credentials and no data-science packages. A session installs
what it needs, for example `uv venv && uv pip install pymc`.

## Build and push

Build from the repository root, because the build reads `uv.lock`:

```bash
docker build -f apps/compute-session/Dockerfile -t registry.example/compute-session .
docker push registry.example/compute-session
docker inspect --format '{{index .RepoDigests 0}}' registry.example/compute-session
```

Put the digest that the last command prints in a profile of
`DAIMON_COMPUTE__PROFILES`.

## Check an image

```bash
docker run --rm -d -p 127.0.0.1:18080:8080 -e MCP_AUTH_TOKEN=smoke registry.example/compute-session
uv run python apps/compute-session/tests/image_smoke.py http://127.0.0.1:18080 smoke
```

The script calls the container through `call_thin_mcp`, the code Daimon uses, and runs a background command to its end. Add `--pymc` to install
PyMC in the container and sample a model.

## Contract

The server listens on every interface, on the port in `PORT` (default 8080). It
does not start without `MCP_AUTH_TOKEN`.

| Request | Answer |
| --- | --- |
| `GET /health` | `200 ok`, no token needed |
| Anything else without `Authorization: Bearer <MCP_AUTH_TOKEN>` | `401`, whatever the path |
| `POST /mcp/` | One JSON-RPC message in, one JSON body out (MCP `2025-03-26`) |

The server keeps no MCP session, so every call can use a new client. It answers
`initialize`, `ping`, `tools/list` and `tools/call`.

| Tool | Arguments | Result |
| --- | --- | --- |
| `run_bash` | `command`, `timeout_s` (1 to 3600, default 40) | `exit_code`, `stdout`, `timed_out`, `truncated` |
| `write_file` | `path`, `content` | `path`, `bytes` |
| `read_file` | `path`, `offset` (default 0), `limit` (default 65536) | `path`, `size`, `offset`, `bytes`, `encoding`, `content`, `eof` |
| `list_files` | `path`, `pattern` (default `*`) | `path`, `count`, `entries` (`name`, `is_dir`, `size`), `truncated` |

Paths are absolute. Each result is in `structuredContent`, and again as JSON
text in `content`.

- `run_bash` runs `bash -c` in the server's working directory with `stderr`
  merged into `stdout`. It returns when the shell ends. When a signal ends the
  command, `exit_code` is 128 plus the signal number, as background commands
  report it. At the timeout it kills the command and everything in the
  command's session, then returns `exit_code` 137 and `timed_out` true.
- `stdout` keeps the first 1 MiB and shows bytes that are not UTF-8 as U+FFFD.
  `list_files` returns at most 1000 entries. `truncated` says when either limit
  cut the result.
- `read_file` chunks, joined, are the file byte for byte. `offset`, `limit` and
  `bytes` count bytes. Read on from `offset + bytes` until `eof` is true.
- `encoding` says how `content` holds a chunk. It is `utf-8` for text, and the
  chunk then ends on a whole character, so it can be up to 3 bytes shorter than
  `limit`. It is `base64` for a chunk that is not UTF-8 text or that holds no
  whole character.
- Daimon stops reading a response at 8 MiB. A `read_file` chunk is at most
  4 MiB and is shorter when the full chunk would not fit.
- A file problem, such as a missing file or a denied write, is a normal result
  with one `error` key. Daimon replaces a failed call with a fixed sentence, so
  a reason sent as a failure would not reach the person. Bad arguments and
  unknown tools do fail the call.

## What a command can reach

The container starts as root, and `tini` stays root as process 1. The server
gives up root before it listens. It then runs as `server` and reads and writes
files as `work`. A command runs as `work` only.

A command cannot signal or trace the server or process 1, and it cannot read
their memory or environment. So it can neither stop the server nor read
`MCP_AUTH_TOKEN`, which the server also removes from the environment that
commands inherit. `tests/image_smoke.py` tries `kill`, `pkill` and `/proc`
reads against a running container.

This protects the server process. It is not a sandbox. A command has the
network and every file of `work`, and it can signal the other commands of its
session. It can also use up memory, disk, process ids or CPU, or lower the
server's scheduling priority, and that can still stop the machine.

If the platform starts the container as a user other than root, the server
cannot separate the two users. It prints a warning and runs commands as its own
user, and a command can then stop it. Use uid 10000 in that case, so that the
home directory stays writable.

## Background commands

Daimon runs long commands detached and tracks them through files in
`/tmp/daimon-tasks`, using scripts it sends through `run_bash`. An image built
some other way needs these for them:

- `bash`, `sh`, `setsid`, `nohup`, `timeout`, `base64`, `tail`, `wc`, `tr` and a
  writable `/tmp`.
- A process 1 that reaps orphaned processes. The status check reads an ended
  command that nobody reaped as still running. This image starts the server
  under `tini` for that reason.

## Where it runs

Daimon runs the image as a Cloud Run service and passes the token. The image
meets what Cloud Run asks of a container: it listens on `PORT` and exits
within about 5 seconds of `SIGTERM`. On Cloud Run the filesystem is memory, and
background commands need CPU that stays allocated between requests. Nothing in
the image depends on one platform: it needs a start as root for the two users,
one port and a writable `/tmp`.

## Tests

```bash
uv run pytest apps/compute-session
```

`tests/test_caller_contract.py` drives the server with Daimon's own caller and
background command scripts, without Docker or a network.
