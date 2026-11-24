"""The thin server over HTTP, in process: auth, the MCP handshake, each tool and its limits."""

from __future__ import annotations

import os
import signal
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import httpx
import pytest
import uvicorn
from compute_session import __main__ as entry
from compute_session import server, tools
from compute_session.server import create_app
from pydantic import JsonValue

from .conftest import HEADERS, TOKEN, URL, Call


def _alive(pid: int) -> bool:
    """Whether a process still runs; one that has ended but is not yet reaped does not."""
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] != "Z"
    except FileNotFoundError:
        return False


@contextmanager
def _left_running(pid: int) -> Iterator[None]:
    """End a process a test left behind on purpose."""
    try:
        yield
    finally:
        if _alive(pid):
            os.kill(pid, signal.SIGKILL)


async def test_health_needs_no_token_and_reveals_nothing(client: httpx.AsyncClient) -> None:
    response = await client.get("https://session.test/health")
    assert (response.status_code, response.text) == (200, "ok")
    assert "www-authenticate" not in response.headers


@pytest.mark.parametrize(
    "authorization",
    [
        None,
        "",
        "Bearer ",
        "Bearer wrong",
        "Bearer " + TOKEN[:-1],
        "Bearer " + TOKEN + "x",
        "bearer " + TOKEN,
        "Basic " + TOKEN,
        TOKEN,
    ],
)
async def test_a_request_without_the_exact_token_runs_nothing(
    client: httpx.AsyncClient, tmp_path: Path, authorization: str | None
) -> None:
    marker = tmp_path / "ran"
    call = {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/call",
        "params": {"name": "run_bash", "arguments": {"command": f"touch {marker}"}},
    }
    headers = {} if authorization is None else {"Authorization": authorization}
    response = await client.post(URL, headers=headers, json=call)
    assert response.status_code == 401
    assert response.content == b"", "a refusal carries no detail"
    assert not marker.exists()
    # The token is checked before the path, so a refusal does not say which paths exist.
    for path in ("/mcp", "/", "/anything"):
        other = await client.post("https://session.test" + path, headers=headers, json=call)
        assert other.status_code == 401, path


def test_an_empty_token_is_never_a_valid_server() -> None:
    for blank in ("", "   "):
        with pytest.raises(ValueError, match="token"):
            create_app(blank)


async def test_a_line_end_after_the_configured_token_is_ignored() -> None:
    transport = httpx.ASGITransport(app=create_app(TOKEN + "\n"))
    async with httpx.AsyncClient(transport=transport) as client:
        ping = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
        assert (await client.post(URL, headers=HEADERS, json=ping)).status_code == 200


def test_the_server_does_not_start_without_the_token(monkeypatch: pytest.MonkeyPatch) -> None:
    def never(*args: object, **kwargs: object) -> None:
        raise AssertionError("the server started without a token")

    monkeypatch.setattr(uvicorn, "run", never)
    for environment in ({}, {"MCP_AUTH_TOKEN": ""}, {"MCP_AUTH_TOKEN": "  "}):
        monkeypatch.delenv("MCP_AUTH_TOKEN", raising=False)
        for name, value in environment.items():
            monkeypatch.setenv(name, value)
        with pytest.raises(SystemExit) as stopped:
            entry.main()
        assert "MCP_AUTH_TOKEN" in str(stopped.value.code)


@pytest.mark.parametrize(("port", "expected"), [(None, 8080), ("9001", 9001)])
def test_the_server_listens_on_every_interface_and_keeps_the_token_from_commands(
    monkeypatch: pytest.MonkeyPatch, port: str | None, expected: int
) -> None:
    started: dict[str, object] = {}

    def run(app: object, **options: object) -> None:
        started.update(options)

    monkeypatch.setattr(uvicorn, "run", run)
    monkeypatch.setenv("MCP_AUTH_TOKEN", TOKEN)
    monkeypatch.delenv("PORT", raising=False)
    if port is not None:
        monkeypatch.setenv("PORT", port)
    entry.main()
    assert (started["host"], started["port"]) == ("0.0.0.0", expected)
    assert "MCP_AUTH_TOKEN" not in os.environ, "commands inherit the server's environment"


async def test_the_handshake_is_stateless_json(client: httpx.AsyncClient) -> None:
    """The three requests daimon.core.compute_fly sends for one call, in its order."""
    headers = dict(HEADERS)
    init = await client.post(
        URL,
        headers=headers,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "daimon-compute", "version": "1"},
            },
        },
    )
    assert init.status_code == 200
    assert init.headers["content-type"] == "application/json"
    assert "mcp-session-id" not in init.headers, "no session to release after the call"
    result = init.json()["result"]
    assert result["protocolVersion"] == "2025-03-26"
    assert result["capabilities"] == {"tools": {}}
    assert result["serverInfo"]["name"] == "daimon-compute-session"

    headers["MCP-Protocol-Version"] = "2025-03-26"
    ready = await client.post(
        URL, headers=headers, json={"jsonrpc": "2.0", "method": "notifications/initialized"}
    )
    assert (ready.status_code, ready.content) == (202, b"")

    called = await client.post(
        URL,
        headers=headers,
        json={
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "run_bash", "arguments": {"command": "echo hi", "timeout_s": 40}},
        },
    )
    assert called.status_code == 200
    message = called.json()
    structured = {"exit_code": 0, "stdout": "hi\n", "timed_out": False, "truncated": False}
    assert message["id"] == 2
    assert message["result"]["structuredContent"] == structured
    assert message["result"]["isError"] is False
    assert [block["type"] for block in message["result"]["content"]] == ["text"]


async def test_an_unknown_protocol_version_is_offered_ours(rpc: Call) -> None:
    result = (await rpc("initialize", {"protocolVersion": "1999-01-01"}))["result"]
    assert isinstance(result, dict) and result["protocolVersion"] == "2025-03-26"


async def test_the_tool_list_is_the_four_session_tools(rpc: Call) -> None:
    result = (await rpc("tools/list", {}))["result"]
    assert isinstance(result, dict) and isinstance(result["tools"], list)
    names = [entry["name"] for entry in result["tools"] if isinstance(entry, dict)]
    assert names == ["run_bash", "write_file", "read_file", "list_files"]
    assert (await rpc("ping", {}))["result"] == {}


async def test_run_bash_is_bash_and_merges_stderr(tool: Call) -> None:
    result = await tool(
        "run_bash", {"command": "[[ -n $BASH_VERSION ]] && echo out; echo err >&2; exit 3"}
    )
    assert result == {
        "exit_code": 3,
        "stdout": "out\nerr\n",
        "timed_out": False,
        "truncated": False,
    }
    assert (await tool("run_bash", {"command": "pwd"}))["stdout"] == os.getcwd() + "\n"


async def test_run_bash_stops_a_command_and_its_children_at_the_timeout() -> None:
    started = time.monotonic()
    result = await tools.run_bash("sleep 30 & echo $!; wait", timeout_s=0.2)
    assert time.monotonic() - started < 5
    assert isinstance(result["stdout"], str)
    child = int(result["stdout"])
    with _left_running(child):
        assert result == {
            "exit_code": -1,
            "stdout": f"{child}\n",
            "timed_out": True,
            "truncated": False,
        }
        deadline = time.monotonic() + 5
        while _alive(child) and time.monotonic() < deadline:
            time.sleep(0.01)
        assert not _alive(child), "the timeout must end what the command started"


async def test_run_bash_returns_when_the_shell_ends_not_when_its_output_closes(tool: Call) -> None:
    """A child left running with the output pipe open must not hold the call until the timeout."""
    started = time.monotonic()
    result = await tool("run_bash", {"command": "sleep 30 & echo $!", "timeout_s": 20})
    assert time.monotonic() - started < 5
    assert isinstance(result["stdout"], str)
    child = int(result["stdout"])
    with _left_running(child):
        assert (result["exit_code"], result["timed_out"]) == (0, False)
        assert _alive(child), "only a timeout ends what a command leaves running"


async def test_run_bash_keeps_the_start_of_long_output(
    tool: Call, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tools, "MAX_STDOUT_BYTES", 1000)
    result = await tool(
        "run_bash", {"command": "printf 'head'; head -c 300000 /dev/zero | tr '\\0' x"}
    )
    assert isinstance(result["stdout"], str)
    assert (len(result["stdout"]), result["stdout"][:5]) == (1000, "headx")
    assert (result["exit_code"], result["truncated"]) == (0, True)


async def test_write_then_read_in_chunks(tool: Call, tmp_path: Path) -> None:
    path = str(tmp_path / "new" / "dir" / "notes.txt")
    text = "naïve café\nline two\n"
    size = len(text.encode())
    assert await tool("write_file", {"path": path, "content": text}) == {
        "path": path,
        "bytes": size,
    }
    assert Path(path).read_bytes() == text.encode()

    whole = await tool("read_file", {"path": path, "offset": 0, "limit": 65536})
    assert whole == {
        "path": path,
        "size": size,
        "offset": 0,
        "bytes": size,
        "content": text,
        "eof": True,
    }
    part = await tool("read_file", {"path": path, "offset": 13, "limit": 4})
    assert (part["content"], part["bytes"], part["eof"]) == ("line", 4, False)
    assert (await tool("read_file", {"path": path}))["content"] == text, (
        "offset and limit are optional"
    )
    past = await tool("read_file", {"path": path, "offset": size + 10, "limit": 4})
    assert (past["content"], past["bytes"], past["eof"]) == ("", 0, True)

    assert await tool("write_file", {"path": path, "content": "x"}) == {"path": path, "bytes": 1}
    assert Path(path).read_text() == "x", "a write replaces the file"


async def test_a_read_that_cannot_fit_the_response_comes_back_shorter(
    tool: Call, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Quotes double in JSON, and the result travels twice, so the chunk shrinks to fit."""
    monkeypatch.setattr(tools, "MAX_RESULT_BYTES", 4000)
    path = tmp_path / "quotes.txt"
    path.write_text('"' * 5000)
    offset, chunks = 0, 0
    while True:
        result = await tool("read_file", {"path": str(path), "offset": offset, "limit": 5000})
        assert len(tools.encode(tools.tool_result(result))) <= 4000
        assert isinstance(result["bytes"], int) and result["bytes"] > 0
        assert result["content"] == '"' * result["bytes"]
        offset, chunks = offset + result["bytes"], chunks + 1
        if result["eof"]:
            break
    assert offset == 5000 and chunks > 1, "following offset + bytes reads the whole file"


async def test_a_file_problem_is_a_result_the_owner_can_read(tool: Call, tmp_path: Path) -> None:
    """The control plane hides a failed call's text, so these must not be failures."""
    (tmp_path / "file").write_text("x")
    missing = str(tmp_path / "missing")
    assert await tool("read_file", {"path": missing}) == {"error": f"not a file: {missing}"}
    assert await tool("list_files", {"path": missing}) == {"error": f"not a directory: {missing}"}
    under_a_file = str(tmp_path / "file" / "child")
    assert await tool("write_file", {"path": under_a_file, "content": "x"}) == {
        "error": f"not a directory: {tmp_path / 'file'}"
    }
    assert await tool("write_file", {"path": str(tmp_path), "content": "x"}) == {
        "error": f"Is a directory: {tmp_path}"
    }
    assert await tool("list_files", {"path": str(tmp_path), "pattern": "/absolute"}) == {
        "error": "not a usable pattern: /absolute"
    }


async def test_list_files_names_sizes_and_patterns(
    tool: Call, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "a.csv").write_text("12345")
    (tmp_path / "b.txt").write_text("")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "c.csv").write_text("1")
    root = str(tmp_path)
    assert await tool("list_files", {"path": root}) == {
        "path": root,
        "count": 3,
        "entries": [
            {"name": "a.csv", "is_dir": False, "size": 5},
            {"name": "b.txt", "is_dir": False, "size": 0},
            {"name": "sub", "is_dir": True, "size": None},
        ],
        "truncated": False,
    }
    nested = await tool("list_files", {"path": root, "pattern": "**/*.csv"})
    assert nested["entries"] == [
        {"name": "a.csv", "is_dir": False, "size": 5},
        {"name": "sub/c.csv", "is_dir": False, "size": 1},
    ]
    monkeypatch.setattr(tools, "MAX_ENTRIES", 2)
    capped = await tool("list_files", {"path": root, "pattern": "*"})
    assert (capped["count"], capped["truncated"]) == (2, True)


@pytest.mark.parametrize(
    ("name", "arguments", "reason"),
    [
        ("run_bash", {}, "command must be a string"),
        ("run_bash", {"command": 7}, "command must be a string"),
        ("run_bash", {"command": "touch {marker}", "timeout_s": 0}, "timeout_s must be"),
        ("run_bash", {"command": "touch {marker}", "timeout_s": 3601}, "timeout_s must be"),
        ("run_bash", {"command": "touch {marker}", "timeout_s": True}, "timeout_s must be"),
        ("run_bash", {"command": "touch {marker}", "timeout_s": "40"}, "timeout_s must be"),
        ("run_bash", {"command": "touch {marker}\x00"}, "NUL"),
        ("write_file", {"path": "relative/{marker}", "content": "x"}, "absolute path"),
        ("write_file", {"path": "{marker}"}, "content must be a string"),
        ("read_file", {"path": "{marker}", "offset": -1}, "offset must be"),
        ("read_file", {"path": "{marker}", "limit": 0}, "limit must be"),
        ("list_files", {"path": "relative"}, "absolute path"),
    ],
)
async def test_bad_arguments_fail_the_call_before_anything_runs(
    rpc: Call, tmp_path: Path, name: str, arguments: dict[str, JsonValue], reason: str
) -> None:
    marker = tmp_path / "ran"
    filled: dict[str, JsonValue] = {
        key: value.replace("{marker}", str(marker)) if isinstance(value, str) else value
        for key, value in arguments.items()
    }
    result = (await rpc("tools/call", {"name": name, "arguments": filled}))["result"]
    assert isinstance(result, dict) and result["isError"] is True
    assert "structuredContent" not in result
    assert reason in str(result["content"])
    assert not marker.exists()


async def test_protocol_errors(
    client: httpx.AsyncClient, rpc: Call, monkeypatch: pytest.MonkeyPatch
) -> None:
    unknown_tool = await rpc("tools/call", {"name": "delete_everything", "arguments": {}})
    assert unknown_tool["error"] == {"code": -32602, "message": "Invalid params"}
    not_an_object = await rpc("tools/call", {"name": "run_bash", "arguments": ["echo"]})
    assert not_an_object["error"] == {"code": -32602, "message": "Invalid params"}
    assert (await rpc("resources/list", {}))["error"] == {
        "code": -32601,
        "message": "Method not found",
    }

    garbage = await client.post(URL, headers=HEADERS, content=b"{not json")
    assert (garbage.status_code, garbage.json()["error"]["code"]) == (400, -32700)
    batch = await client.post(URL, headers=HEADERS, json=[{"jsonrpc": "2.0", "id": 1}])
    assert (batch.status_code, batch.json()["error"]["code"]) == (400, -32600)

    get = await client.get(URL, headers=HEADERS)
    assert (get.status_code, get.headers["allow"]) == (405, "POST")
    assert (await client.delete(URL, headers=HEADERS)).status_code == 405
    assert (await client.post("https://session.test/other", headers=HEADERS)).status_code == 404

    monkeypatch.setattr(server, "MAX_REQUEST_BYTES", 100)
    too_big = await client.post(URL, headers=HEADERS, content=b" " * 101)
    assert too_big.status_code == 413
