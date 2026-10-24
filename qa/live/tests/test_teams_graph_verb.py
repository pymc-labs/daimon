"""Offline contracts for the raw Graph verb."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from qa.live.teams import __main__ as cli
from qa.live.teams.config import TeamsQaConfig
from qa.live.teams.graph import GraphTeamsDriver
from qa.live.teams.harness import graph

TOKEN = "SECRET_ACCESS_TOKEN"


class FakeTokens:
    def __init__(self) -> None:
        self.scopes: list[list[str]] = []

    def acquire(self, persona: str, scopes: list[str]) -> SimpleNamespace:
        self.scopes.append(scopes)
        return SimpleNamespace(value=TOKEN)


def driver(
    config: TeamsQaConfig, status: int = 200, body: Any = None
) -> tuple[GraphTeamsDriver, list[httpx.Request]]:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(status, json=body if body is not None else {"value": []})

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    graph_driver = GraphTeamsDriver(config, FakeTokens(), http)  # type: ignore[arg-type]

    async def bot_chat(persona: str) -> tuple[str, list[dict[str, Any]]]:
        return "19:bot-chat@unq.gbl.spaces", []

    graph_driver.bot_chat = bot_chat  # type: ignore[method-assign]
    return graph_driver, requests


@pytest.fixture
def config() -> TeamsQaConfig:
    from pathlib import Path

    source = json.loads((Path(__file__).parents[1] / "teams/config.example.json").read_text())
    source.pop("token_store", None)
    source["token_dir"] = "/tmp/teams-qa-graph-test"
    return TeamsQaConfig.model_validate(source)


@pytest.mark.asyncio
async def test_get_anywhere_returns_status_and_body(config: TeamsQaConfig) -> None:
    graph_driver, requests = driver(config, body={"value": [{"id": "t1"}]})
    result = await graph(graph_driver, "admin", "get", "/me/joinedTeams")
    assert result == {
        "ok": True,
        "persona": "admin",
        "status": 200,
        "body": {"value": [{"id": "t1"}]},
    }
    assert str(requests[0].url) == "https://graph.microsoft.com/v1.0/me/joinedTeams"
    assert requests[0].headers["Authorization"] == f"Bearer {TOKEN}"
    assert TOKEN not in json.dumps(result)
    assert graph_driver.tokens.scopes == [["https://graph.microsoft.com/.default"]]  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_beta_and_full_urls(config: TeamsQaConfig) -> None:
    graph_driver, requests = driver(config)
    await graph(graph_driver, "admin", "GET", "/beta/me/chats")
    await graph(graph_driver, "admin", "GET", "https://graph.microsoft.com/v1.0/me")
    assert [str(r.url) for r in requests] == [
        "https://graph.microsoft.com/beta/me/chats",
        "https://graph.microsoft.com/v1.0/me",
    ]


@pytest.mark.asyncio
async def test_writes_inside_qa_team_and_bot_chat(config: TeamsQaConfig) -> None:
    graph_driver, requests = driver(config, status=201, body={"id": "new"})
    team = f"/teams/{config.team_id}"
    body = json.dumps({"displayName": "qa-scratch", "membershipType": "standard"})
    created = await graph(graph_driver, "admin", "POST", f"{team}/channels", body)
    posted = await graph(
        graph_driver, "member", "POST", "/chats/19:bot-chat@unq.gbl.spaces/messages", "{}"
    )
    renamed = await graph(graph_driver, "admin", "PATCH", team, '{"description": "qa"}')
    assert [created["status"], posted["status"], renamed["status"]] == [201, 201, 201]
    assert json.loads(requests[0].content) == json.loads(body)


@pytest.mark.asyncio
async def test_upload_puts_raw_bytes_into_the_qa_team_files(config: TeamsQaConfig) -> None:
    graph_driver, requests = driver(config, status=201, body={"id": "item"})
    path = f"/groups/{config.team_id}/drive/root:/General/weekly_sales.csv:/content"
    result = await graph(graph_driver, "admin", "PUT", path, upload=(b"week,sales\n", "text/csv"))
    assert result["status"] == 201
    assert requests[0].content == b"week,sales\n"
    assert requests[0].headers["Content-Type"] == "text/csv"


@pytest.mark.parametrize(
    ("method", "suffix"),
    [
        ("POST", "items/abc/invite"),
        ("POST", "root/createLink"),
        ("POST", "items/abc/copy"),
        ("PATCH", "items/abc/permissions/p1"),
        ("PUT", "items/abc/permissions/p1"),
        ("PUT", "root:/General/a.csv:/content/../../x"),
    ],
)
@pytest.mark.asyncio
async def test_the_team_library_takes_uploads_only(
    config: TeamsQaConfig, method: str, suffix: str
) -> None:
    graph_driver, requests = driver(config)
    path = f"/groups/{config.team_id}/drive/{suffix}"
    shared = await graph(graph_driver, "admin", method, path, "{}")
    as_upload = await graph(graph_driver, "admin", "PUT", path, upload=(b"x", "text/csv"))
    assert shared["ok"] is False and as_upload["ok"] is False
    assert requests == []


@pytest.mark.parametrize(
    "path",
    [
        "/groups/{team}/drive/%2e%2e/%2e%2e/users/v/drive/root:/a:/content",
        "/groups/{team}/drive/%252e%252e/x/content",
        "/teams/{team}/channels/a%2F..%2Fb/messages",
        "/teams/{team}/channels/a%2Fb/messages",
        "/groups/{team}/drive/root:/x%3A/invite#:/content",
    ],
)
@pytest.mark.asyncio
async def test_encoded_climbs_and_separators_are_refused(config: TeamsQaConfig, path: str) -> None:
    graph_driver, requests = driver(config)
    result = await graph(graph_driver, "admin", "GET", path.format(team=config.team_id))
    assert result["error"] == "invalid_path"
    assert requests == []


@pytest.mark.asyncio
async def test_upload_refuses_other_drives_and_a_second_body(config: TeamsQaConfig) -> None:
    graph_driver, requests = driver(config)
    csv = (b"x", "text/csv")
    elsewhere = await graph(
        graph_driver, "admin", "PUT", "/me/drive/root:/a.csv:/content", upload=csv
    )
    other_group = await graph(
        graph_driver, "admin", "PUT", "/groups/other/drive/root:/a.csv:/content", upload=csv
    )
    library = f"/groups/{config.team_id}/drive/root:/General/a.csv:/content"
    both = await graph(graph_driver, "admin", "PUT", library, "{}", upload=csv)
    posted = await graph(graph_driver, "admin", "POST", library, upload=csv)
    assert [elsewhere["error"], other_group["error"], both["error"], posted["error"]] == [
        "write_outside_qa_team",
        "write_outside_qa_team",
        "invalid_body",
        "invalid_body",
    ]
    assert requests == []


@pytest.mark.parametrize(
    ("method", "path", "error"),
    [
        ("POST", "/teams/other-team/channels", "write_outside_qa_team"),
        ("POST", "/chats", "write_outside_qa_team"),
        ("POST", "/chats/19:someone-else/messages", "write_outside_qa_team"),
        ("PATCH", "/me/presence", "write_outside_qa_team"),
        ("DELETE", "/users/someone", "write_outside_qa_team"),
        ("DELETE", "/teams/{team}", "qa_team_protected"),
        ("POST", "/teams/{team}/archive", "qa_team_protected"),
        ("POST", "/teams/{team}/../other/channels", "invalid_path"),
        ("GET", "https://evil.example/me", "invalid_path"),
        ("TRACE", "/me", "invalid_method"),
    ],
)
@pytest.mark.asyncio
async def test_refusals_make_no_request(
    config: TeamsQaConfig, method: str, path: str, error: str
) -> None:
    graph_driver, requests = driver(config)
    result = await graph(graph_driver, "admin", method, path.format(team=config.team_id), "{}")
    assert result == {"ok": False, "error": error, "persona": "admin"}
    assert requests == []


@pytest.mark.asyncio
async def test_invalid_body_and_http_error(config: TeamsQaConfig) -> None:
    graph_driver, requests = driver(config, status=403, body={"error": {"code": "Forbidden"}})
    bad = await graph(graph_driver, "admin", "POST", f"/teams/{config.team_id}/channels", "{")
    assert bad["error"] == "invalid_body"
    assert requests == []
    denied = await graph(graph_driver, "admin", "GET", "/me")
    assert denied == {
        "ok": False,
        "persona": "admin",
        "status": 403,
        "body": {"error": {"code": "Forbidden"}},
    }


def test_cli_wires_graph(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls: list[tuple[Any, ...]] = []

    async def fake_graph(
        driver: Any,
        persona: str,
        method: str,
        path: str,
        body: str | None,
        *,
        upload: tuple[bytes, str] | None = None,
    ) -> dict[str, Any]:
        calls.append((persona, method, path, body, upload))
        return {"ok": True}

    monkeypatch.setattr(cli, "graph", fake_graph)
    monkeypatch.setattr(
        cli.TeamsQaConfig, "load", lambda *a, **k: SimpleNamespace(personas={"admin": None})
    )
    monkeypatch.setattr(cli, "TokenStore", lambda config: None)
    monkeypatch.setattr(cli, "GraphTeamsDriver", lambda *a, **k: None)
    code = cli.main(
        ["graph", "--persona", "admin", "--method", "POST", "/teams/t/channels", "--body", "{}"]
    )
    assert code == 0
    assert calls == [("admin", "POST", "/teams/t/channels", "{}", None)]
    with pytest.raises(SystemExit):
        cli.main(["read", "--persona", "admin", "--method", "POST"])
    with pytest.raises(SystemExit):
        cli.main(["graph", "--persona", "admin"])


def test_cli_sends_a_body_file_as_raw_bytes(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    uploads: list[tuple[bytes, str] | None] = []

    async def fake_graph(*args: Any, upload: tuple[bytes, str] | None = None) -> dict[str, Any]:
        uploads.append(upload)
        return {"ok": True}

    monkeypatch.setattr(cli, "graph", fake_graph)
    monkeypatch.setattr(
        cli.TeamsQaConfig, "load", lambda *a, **k: SimpleNamespace(personas={"admin": None})
    )
    monkeypatch.setattr(cli, "TokenStore", lambda config: None)
    monkeypatch.setattr(cli, "GraphTeamsDriver", lambda *a, **k: None)
    data = tmp_path / "weekly_sales.csv"
    data.write_bytes(b"week,sales\n")
    path = "/groups/t/drive/root:/General/weekly_sales.csv:/content"
    argv = ["graph", "--persona", "admin", "--method", "PUT", path, "--body-file", str(data)]
    assert cli.main(argv) == 0
    assert uploads == [(b"week,sales\n", "text/csv")]
    with pytest.raises(SystemExit):
        cli.main([*argv, "--body", "{}"])
    with pytest.raises(SystemExit):
        cli.main([*argv[:4], "POST", *argv[5:]])
    with pytest.raises(SystemExit):
        cli.main([*argv[:-1], str(tmp_path / "missing.csv")])
