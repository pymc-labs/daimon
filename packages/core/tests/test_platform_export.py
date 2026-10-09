"""Recovery archives through the real SDK and fake HTTP; no paid API calls."""

import hashlib
import json
import zipfile
from pathlib import Path

import httpx
import pytest
from anthropic import APIStatusError, AsyncAnthropic
from daimon.core.defaults.platform_export import export_platform
from daimon.core.errors import SkillsListTruncatedError


class ExportTransport:
    def __init__(self, *, fail_download=False, truncated=False):
        self.fail_download = fail_download
        self.truncated = truncated
        self.requests = []

    def __call__(self, request):
        self.requests.append(request)
        assert request.method == "GET", "exports must never mutate platform state"
        path = request.url.path
        page = request.url.params.get("page")
        agent = {
            "id": "agent1",
            "name": "../same name",
            "system": "instructions",
            "metadata": {},
            "model": {"id": "claude-sonnet-4-6"},
            "tools": [],
            "mcp_servers": [],
            "skills": [],
            "type": "agent",
            "version": 1,
            "created_at": "2026-09-28T00:00:00Z",
            "updated_at": "2026-09-28T00:00:00Z",
        }
        skill = {"id": "skill1", "source": "custom", "type": "skill"}
        memory = {
            "id": "mem1",
            "type": "memory",
            "path": "/notes/facts",
            "content": "remember this",
        }
        if path == "/v1/agents":
            return httpx.Response(
                200,
                json={
                    "data": [dict(agent, id="agent2") if page else agent],
                    "next_page": None if page else "second",
                },
            )
        if path.startswith("/v1/agents/"):
            return httpx.Response(200, json=dict(agent, id=path.rsplit("/", 1)[1]))
        if path == "/v1/environments":
            data = [{"id": "env1"}]
        elif path == "/v1/environments/env1":
            return httpx.Response(
                200, json={"id": "env1", "name": "python", "config": {"type": "cloud"}}
            )
        elif path == "/v1/skills":
            data = [skill] * (int(request.url.params["limit"]) if self.truncated else 1)
        elif path == "/v1/skills/skill1/versions":
            data = [
                {"id": "skill_version_1", "version": "1"},
                {"id": "skill_version_2", "version": "2"},
            ]
        elif path.endswith("/content"):
            refusal = workspace_key_download_refusal(request)
            if refusal is not None:
                return refusal
            if self.fail_download:
                return httpx.Response(
                    500, json={"error": {"type": "api_error", "message": "unavailable"}}
                )
            return httpx.Response(200, content=b"skill zip")
        elif path == "/v1/memory_stores":
            data = [{"id": "store1"}]
        elif path == "/v1/memory_stores/store1/memories":
            data = [memory]
        elif path == "/v1/memory_stores/store1/memories/mem1":
            assert request.url.params["view"] == "full"
            return httpx.Response(200, json=memory)
        else:
            raise AssertionError(f"Unexpected export route: {path}")
        return httpx.Response(200, json={"data": data, "next_page": None})


def workspace_key_download_refusal(request: httpx.Request) -> httpx.Response | None:
    """Answer a skill download as a production workspace API key gets it."""
    if "skills-2025-10-02" in request.headers.get("anthropic-beta", ""):
        message = "Downloading skill content is not supported with a workspace API key"
        return httpx.Response(403, json={"error": {"type": "permission_error", "message": message}})
    if "/versions/skill_version_" not in request.url.path:
        message = "Invalid version id"
        return httpx.Response(
            400, json={"error": {"type": "invalid_request_error", "message": message}}
        )
    return None


def export_client(transport):
    return AsyncAnthropic(
        api_key="test-key",
        max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(transport)),
    )


async def test_export_preserves_all_pages_payloads_and_integrity(tmp_path: Path):
    target = tmp_path / "export.zip"
    transport = ExportTransport()
    async with export_client(transport) as client:
        await export_platform(client, target)
    assert target.stat().st_mode & 0o777 == 0o600
    with zipfile.ZipFile(target) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["format"] == "daimon-platform-export-v1"
        for name, digest in manifest["sha256"].items():
            assert ".." not in name
            assert hashlib.sha256(archive.read(name)).hexdigest() == digest
        names = archive.namelist()
        assert len([n for n in names if n.startswith("agents/")]) == 2
        assert len([n for n in names if n.endswith("content.zip")]) == 2
        memories = [json.loads(archive.read(n)) for n in names if n.startswith("memory-stores/")]
        assert any(m.get("content") == "remember this" for m in memories)
    assert not list(tmp_path.glob(".platform-export-*"))


@pytest.mark.parametrize("truncated", [False, True])
async def test_export_failure_never_publishes_partial_archive(tmp_path: Path, truncated):
    transport = ExportTransport(fail_download=not truncated, truncated=truncated)
    async with export_client(transport) as client:
        with pytest.raises(SkillsListTruncatedError if truncated else APIStatusError):
            await export_platform(client, tmp_path / "export.zip")
    assert list(tmp_path.iterdir()) == []


async def test_export_refuses_overwrite(tmp_path: Path):
    target = tmp_path / "export.zip"
    target.write_bytes(b"previous good backup")
    async with export_client(ExportTransport()) as client:
        with pytest.raises(FileExistsError):
            await export_platform(client, target)
    assert target.read_bytes() == b"previous good backup"
    assert list(tmp_path.iterdir()) == [target]
