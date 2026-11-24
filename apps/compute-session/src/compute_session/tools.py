"""The four session tools and the shape of their results.

Each tool returns a JSON object that the control plane relays as it is. A failure
the session owner can act on (a missing file, a denied write) is an ``error`` key
in a normal result: the control plane replaces a failed tool call with one fixed
sentence, so a reason reported as a failure would never reach the owner.
"""

from __future__ import annotations

import asyncio
import base64
import codecs
import contextlib
import itertools
import json
import os
import signal
import stat
from collections.abc import Callable, Mapping
from pathlib import Path

type Json = None | bool | int | float | str | list[Json] | dict[str, Json]
type Result = dict[str, Json]

MIB = 1024 * 1024
#: The control plane stops reading a response body at 8 MiB. A result travels twice
#: in that body, as text and as structured content; the rest is left to the envelope.
MAX_RESULT_BYTES = 7 * MIB
MAX_READ_BYTES = 4 * MIB
DEFAULT_READ_BYTES = 64 * 1024
MAX_STDOUT_BYTES = MIB
MAX_ENTRIES = 1000
DEFAULT_TIMEOUT_S = 40
MAX_TIMEOUT_S = 3600
#: How long to keep reading after the shell exits while something it left running
#: still holds the output pipe open.
DRAIN_GRACE_S = 0.2
_MAX_OFFSET = 2**63 - 1

_PATH: Json = {"type": "string", "description": "Absolute path."}
TOOLS: list[Json] = [
    {
        "name": "run_bash",
        "description": (
            "Run a command with bash and wait for it to end. Returns exit_code (128 + "
            "the signal number when a signal ended it), stdout (stderr included), "
            "timed_out and truncated."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "command": {"type": "string"},
                "timeout_s": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": MAX_TIMEOUT_S,
                    "default": DEFAULT_TIMEOUT_S,
                },
            },
            "required": ["command"],
        },
    },
    {
        "name": "write_file",
        "description": "Write UTF-8 text to a file, replacing it. Creates parent directories.",
        "inputSchema": {
            "type": "object",
            "properties": {"path": _PATH, "content": {"type": "string"}},
            "required": ["path", "content"],
        },
    },
    {
        "name": "read_file",
        "description": (
            "Read a chunk of a file. offset and limit are in bytes. A chunk ends on a "
            "whole character and can be shorter than limit: continue at offset + bytes "
            "until eof is true. encoding says how content holds the bytes: utf-8 text, "
            "or base64 when the chunk is not UTF-8 text."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": _PATH,
                "offset": {"type": "integer", "minimum": 0, "default": 0},
                "limit": {"type": "integer", "minimum": 1, "default": DEFAULT_READ_BYTES},
            },
            "required": ["path"],
        },
    },
    {
        "name": "list_files",
        "description": (
            f"List the entries of a directory that match a glob pattern, at most {MAX_ENTRIES}."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"path": _PATH, "pattern": {"type": "string", "default": "*"}},
            "required": ["path"],
        },
    },
]


class InvalidArguments(ValueError):
    """The call named no tool of ours or carried arguments a tool cannot use."""


def encode(payload: Json) -> bytes:
    """The one JSON encoding of a response body, so size checks measure what is sent."""
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return text.encode("utf-8", "replace")


def tool_result(result: Result) -> Result:
    """An MCP tool result: the object, and its JSON text for clients that read text."""
    return {
        "content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}],
        "structuredContent": result,
        "isError": False,
    }


def tool_error(message: str) -> Result:
    return {"content": [{"type": "text", "text": message}], "isError": True}


def _fit(size: int, build: Callable[[int], Result]) -> Result:
    """The result built from the longest prefix that fits a response, halving to find it."""
    while True:
        result = build(size)
        if size == 0 or len(encode(tool_result(result))) <= MAX_RESULT_BYTES:
            return result
        size //= 2


class _Capture:
    """Keeps the first ``limit`` bytes of a stream and reads the rest away."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.kept = bytearray()
        self.dropped = False

    async def drain(self, stream: asyncio.StreamReader) -> None:
        while chunk := await stream.read(64 * 1024):
            room = self.limit - len(self.kept)
            self.kept += chunk[:room]
            self.dropped = self.dropped or len(chunk) > room


def _kill_group(pid: int) -> None:
    """End a command and everything it started in its own session."""
    with contextlib.suppress(ProcessLookupError):
        os.killpg(pid, signal.SIGKILL)


def _whole_characters(data: bytes, errors: str = "strict") -> tuple[str, int]:
    """The text of ``data`` up to its last whole UTF-8 character, and its length in bytes.

    Bytes are cut where a size limit falls, which can be inside a character. The
    unfinished character stays out, so the next chunk starts with all of it.
    """
    decoder = codecs.getincrementaldecoder("utf-8")(errors)
    text = decoder.decode(data)
    return text, len(data) - len(decoder.getstate()[0])


async def run_bash(command: str, timeout_s: float) -> Result:
    """Run ``command`` and wait for the shell, not for what it leaves running.

    The command gets its own session. At the timeout that whole session is killed;
    a process the command moved out with ``setsid`` is not part of it and survives.
    """
    # A server that holds two identities (see ``__main__``) answers as one user and
    # runs commands as another. The command keeps only the second: with the first
    # it could signal the server.
    two_users = os.getuid() != os.geteuid()
    read_fd, write_fd = os.pipe()
    pipe = os.fdopen(read_fd, "rb", buffering=0)
    try:
        # The pipe is ours, not one asyncio attaches to the process: waiting on a
        # process with attached pipes also waits for every holder of those pipes,
        # so a detached child would keep the call open until the timeout.
        process = await asyncio.create_subprocess_exec(
            "bash",
            "-c",
            command,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=write_fd,
            stderr=write_fd,
            start_new_session=True,
            user=os.geteuid() if two_users else None,
            group=os.getegid() if two_users else None,
        )
    except BaseException:
        pipe.close()
        raise
    finally:
        os.close(write_fd)
    stream = asyncio.StreamReader()
    transport, _ = await asyncio.get_running_loop().connect_read_pipe(
        lambda: asyncio.StreamReaderProtocol(stream), pipe
    )
    capture = _Capture(MAX_STDOUT_BYTES)
    reader = asyncio.create_task(capture.drain(stream))
    timed_out = False
    try:
        try:
            async with asyncio.timeout(timeout_s):
                await process.wait()
        except TimeoutError:
            timed_out = True
            _kill_group(process.pid)
            await process.wait()
        with contextlib.suppress(TimeoutError):
            async with asyncio.timeout(DRAIN_GRACE_S):
                await reader
    finally:
        if process.returncode is None:
            # Cancelled while waiting (the server is shutting down).
            _kill_group(process.pid)
        reader.cancel()
        transport.close()
    output = bytes(capture.kept)
    ended = process.returncode
    assert ended is not None
    # A signal death as a shell reports it, which is what the background path records.
    exit_code = 128 - ended if ended < 0 else ended

    def build(keep: int) -> Result:
        truncated = capture.dropped or keep < len(output)
        # Output is shown, not stored: bytes that are not UTF-8 become U+FFFD.
        text = (
            _whole_characters(output[:keep], "replace")[0]
            if truncated
            else output.decode("utf-8", "replace")
        )
        return {
            "exit_code": exit_code,
            "stdout": text,
            "timed_out": timed_out,
            "truncated": truncated,
        }

    return _fit(len(output), build)


def _reason(error: OSError, path: str) -> Result:
    return {"error": f"{error.strerror or type(error).__name__}: {path}"}


def write_file(path: str, content: str) -> Result:
    target = Path(path)
    data = content.encode()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    except FileExistsError:
        # mkdir reports a file standing where a parent directory is needed this way.
        return {"error": f"not a directory: {target.parent}"}
    except OSError as error:
        return _reason(error, path)
    return {"path": str(target), "bytes": len(data)}


def _chunk(piece: bytes) -> tuple[str, str, int]:
    """How a chunk travels: its encoding, its content and the bytes that content holds.

    UTF-8 text travels as text up to its last whole character. Bytes that are not
    UTF-8 text, or that hold no whole character, travel whole as base64.
    """
    try:
        text, used = _whole_characters(piece)
    except UnicodeDecodeError:
        text, used = "", 0
    if used or not piece:
        return "utf-8", text, used
    return "base64", base64.b64encode(piece).decode("ascii"), len(piece)


def read_file(path: str, offset: int, limit: int) -> Result:
    """Read one chunk so that the chunks of a file, joined, are the file byte for byte."""
    target = Path(path)
    try:
        if not target.is_file():
            return {"error": f"not a file: {path}"}
        with target.open("rb") as handle:
            handle.seek(offset)
            data = handle.read(min(limit, MAX_READ_BYTES))
            # Read after the chunk, so a file that grows meanwhile is not cut short.
            size = max(os.fstat(handle.fileno()).st_size, offset + len(data))
    except OSError as error:
        return _reason(error, path)

    def build(keep: int) -> Result:
        encoding, content, used = _chunk(data[:keep])
        return {
            "path": str(target),
            "size": size,
            "offset": offset,
            "bytes": used,
            "encoding": encoding,
            "content": content,
            "eof": offset + used >= size,
        }

    return _fit(len(data), build)


def list_files(path: str, pattern: str) -> Result:
    root = Path(path)
    try:
        if not root.is_dir():
            return {"error": f"not a directory: {path}"}
        found = sorted(itertools.islice(root.glob(pattern), MAX_ENTRIES + 1))
    except OSError as error:
        return _reason(error, path)
    except (ValueError, NotImplementedError):
        return {"error": f"not a usable pattern: {pattern}"}
    entries: list[Json] = []
    for child in found[:MAX_ENTRIES]:
        try:
            mode, size = (info := child.stat()).st_mode, info.st_size
        except OSError:
            continue  # A broken link, or an entry removed while listing.
        # A file name is bytes on disk; one that is not UTF-8 cannot travel as JSON text.
        name = os.fsencode(child.relative_to(root)).decode("utf-8", "replace")
        entries.append(
            {
                "name": name,
                "is_dir": stat.S_ISDIR(mode),
                "size": size if stat.S_ISREG(mode) else None,
            }
        )
    return {
        "path": str(root),
        "count": len(entries),
        "entries": entries,
        "truncated": len(found) > MAX_ENTRIES,
    }


def _string(arguments: Mapping[str, Json], key: str, default: str | None = None) -> str:
    value = arguments.get(key, default)
    if not isinstance(value, str):
        raise InvalidArguments(f"{key} must be a string")
    try:
        value.encode()
    except UnicodeEncodeError:
        raise InvalidArguments(f"{key} must be valid Unicode text") from None
    return value


def _line(arguments: Mapping[str, Json], key: str, default: str | None = None) -> str:
    """A string the operating system takes as one argument or one path."""
    value = _string(arguments, key, default)
    if "\x00" in value:
        raise InvalidArguments(f"{key} must not contain a NUL character")
    return value


def _absolute(arguments: Mapping[str, Json], key: str) -> str:
    value = _line(arguments, key)
    if not value.startswith("/"):
        raise InvalidArguments(f"{key} must be an absolute path")
    return value


def _whole(arguments: Mapping[str, Json], key: str, default: int, low: int, high: int) -> int:
    value = arguments.get(key)
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise InvalidArguments(f"{key} must be a whole number from {low} to {high}")
    return value


async def call_tool(name: str, arguments: Mapping[str, Json]) -> Result:
    """Check the arguments, then run the tool. Nothing runs when a check fails."""
    if name == "run_bash":
        command = _line(arguments, "command")
        timeout_s = _whole(arguments, "timeout_s", DEFAULT_TIMEOUT_S, 1, MAX_TIMEOUT_S)
        return await run_bash(command, timeout_s)
    if name == "write_file":
        path, content = _absolute(arguments, "path"), _string(arguments, "content")
        return await asyncio.to_thread(write_file, path, content)
    if name == "read_file":
        path = _absolute(arguments, "path")
        offset = _whole(arguments, "offset", 0, 0, _MAX_OFFSET)
        limit = _whole(arguments, "limit", DEFAULT_READ_BYTES, 1, _MAX_OFFSET)
        return await asyncio.to_thread(read_file, path, offset, limit)
    if name == "list_files":
        path, pattern = _absolute(arguments, "path"), _line(arguments, "pattern", "*")
        return await asyncio.to_thread(list_files, path, pattern)
    raise InvalidArguments(f"unknown tool: {name}")
