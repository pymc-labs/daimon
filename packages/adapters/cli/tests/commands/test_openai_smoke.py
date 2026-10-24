"""`daimon openai smoke` runs one real chat turn, with one skill and one tool
call, against a fake OpenAI transport and asserts the pass/fail contract."""

from __future__ import annotations

import io
import json
import zipfile

import httpx
import pytest
import typer
from daimon.adapters.cli.commands.openai import openai_smoke_command
from mux.contracts.resources import SkillUpload, SkillUploadFile
from mux.drivers.openai.skills import bundle_digest
from mux.drivers.openai.transport import SDKTransport
from mux.testing.isolated_sdk import build_async_openai

SKILL_CONTENT = b"""---
name: daimon-smoke
description: Smoke-test skill used only by `daimon openai smoke`.
---

# Daimon smoke skill

This skill exists only to prove skill upload, download and digest
verification work end to end against the real OpenAI API.
"""


_BUNDLE = SkillUpload(files=(SkillUploadFile(path="SKILL.md", content=SKILL_CONTENT),))
_DIGEST = bundle_digest(_BUNDLE)


def packed_skill() -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("wrapper/SKILL.md", SKILL_CONTENT)
    return output.getvalue()


class FakeOpenAI:
    def __init__(
        self,
        *,
        tool_called_eventually: bool = True,
        content_404_once: bool = False,
        create_returns_idle_placeholder: bool = False,
    ):
        self.calls: list[httpx.Request] = []
        self.tool_called_eventually = tool_called_eventually
        self.content_404_once = content_404_once
        self.create_returns_idle_placeholder = create_returns_idle_placeholder
        self._content_calls = 0
        self._retrieve_calls = 0
        self._events_posted = False
        self._digest = _DIGEST

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        path = request.url.path
        body = (
            json.loads(request.content or b"{}")
            if request.headers.get("content-type", "").startswith("application/json")
            else {}
        )

        if path == "/v1/skills":
            return httpx.Response(
                200,
                json={
                    "id": "skill-1",
                    "name": "bundle",
                    "latest_version": "1",
                    "created_at": 1700000000,
                },
            )
        if path == "/v1/skills/skill-1/versions":
            return httpx.Response(
                200,
                json={
                    "skill_id": "skill-1",
                    "version": "1",
                    "name": "bundle",
                    "created_at": 1700000000,
                },
            )
        if path == "/v1/skills/skill-1/versions/1":
            return httpx.Response(200, json={"skill_id": "skill-1", "version": "1"})
        if path == "/v1/skills/skill-1/versions/1/content":
            self._content_calls += 1
            if self.content_404_once and self._content_calls == 1:
                return httpx.Response(404, json={"error": {"message": "not found"}})
            return httpx.Response(200, content=packed_skill())
        if path == "/v1/agents":
            return httpx.Response(
                200,
                json={
                    "id": "agent-123",
                    "model": body["model"],
                    "name": body.get("name", ""),
                    "metadata": body.get("metadata", {}),
                    "tools": body.get("tools", []),
                    "created_at": 1700000000,
                },
            )
        if path == "/v1/agents/agent-123":
            return httpx.Response(
                200,
                json={
                    "id": "agent-123",
                    "model": "gpt-6-luna",
                    "name": "daimon-smoke",
                    "metadata": {
                        "mux_tenant": "any",
                        "mux_skill_pins": json.dumps(
                            [{"id": "skill-1", "version": "1", "digest": self._digest}]
                        ),
                    },
                    "tools": [],
                    "created_at": 1700000000,
                },
            )
        if path == "/v1/agents/environments/templates":
            return httpx.Response(
                200, json={"id": "env-123", "name": body.get("name", ""), "created_at": 1700000000}
            )
        if path == "/v1/agents/sessions":
            if self.create_returns_idle_placeholder:
                return httpx.Response(
                    200,
                    json={
                        "id": "session-1",
                        "status": "idle",
                        "agent": {"id": "agent-123"},
                        "environment": {"id": "env-123", "type": "openai_hosted"},
                        "metadata": {"mux_tenant": "any"},
                    },
                )
            return httpx.Response(
                200,
                json={
                    "id": "session-1",
                    "status": "requires_action",
                    "agent": {"id": "agent-123"},
                    "environment": {"id": "env-123", "type": "openai_hosted"},
                    "metadata": {"mux_tenant": "any"},
                    "required_actions": [
                        {"type": "function_call", "call_id": "call-1", "turn_id": "turn-1"}
                    ],
                },
            )
        if path == "/v1/agents/sessions/session-1":
            self._retrieve_calls += 1
            if self._events_posted and self.tool_called_eventually:
                return httpx.Response(
                    200,
                    json={
                        "id": "session-1",
                        "status": "idle",
                        "agent": {"id": "agent-123"},
                        "environment": {"id": "env-123", "type": "openai_hosted"},
                        "metadata": {"mux_tenant": "any"},
                    },
                )
            return httpx.Response(
                200,
                json={
                    "id": "session-1",
                    "status": "requires_action",
                    "agent": {"id": "agent-123"},
                    "environment": {"id": "env-123", "type": "openai_hosted"},
                    "metadata": {"mux_tenant": "any"},
                    "required_actions": [
                        {"type": "function_call", "call_id": "call-1", "turn_id": "turn-1"}
                    ],
                },
            )
        if path == "/v1/agents/sessions/session-1/turns":
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": "turn-1",
                            "session_id": "session-1",
                            "subagent_id": None,
                            "status": "in_progress",
                        }
                    ]
                },
            )
        if path == "/v1/agents/sessions/session-1/events":
            self._events_posted = True
            return httpx.Response(200, json={})
        raise AssertionError(f"unexpected request: {request.method} {path}")

    def transport_factory(self):
        def factory(*, api_key: str, project: str):
            del api_key, project
            return SDKTransport(build_async_openai(transport=httpx.MockTransport(self.handle)))

        return factory


@pytest.fixture(autouse=True)
def _fast_sleep(monkeypatch: pytest.MonkeyPatch):
    async def _no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr("daimon.adapters.cli.commands.openai.asyncio.sleep", _no_sleep)


def test_smoke_completes_one_tool_call_and_reports_ok(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("DAIMON_CHANNEL_OPENAI_KEY", "sk-test-not-a-real-key")
    fake = FakeOpenAI()
    monkeypatch.setattr(
        "daimon.adapters.cli.commands.openai.configured_transport", fake.transport_factory()
    )

    openai_smoke_command(project="proj_123", model="gpt-6-luna")

    out = capsys.readouterr().out
    assert "smoke: OK" in out
    assert "tool_calls=1" in out
    assert "sk-test-not-a-real-key" not in out


def test_smoke_ignores_the_misleading_idle_status_on_session_create(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Regression check: live against the real OpenAI API, `sessions.create`'s
    own response can read status="idle" with no required actions even though
    the initial message was just queued — the turn hasn't started yet. A
    smoke command that trusted this immediately would report a false "0 tool
    calls" pass/fail a fraction of a second before the real work started."""
    monkeypatch.setenv("DAIMON_CHANNEL_OPENAI_KEY", "sk-test-not-a-real-key")
    fake = FakeOpenAI(create_returns_idle_placeholder=True)
    monkeypatch.setattr(
        "daimon.adapters.cli.commands.openai.configured_transport", fake.transport_factory()
    )

    openai_smoke_command(project="proj_123", model="gpt-6-luna")

    out = capsys.readouterr().out
    assert "smoke: OK" in out
    assert "tool_calls=1" in out
    assert fake._retrieve_calls >= 1


def test_smoke_never_called_times_out_and_exits_nonzero(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("DAIMON_CHANNEL_OPENAI_KEY", "sk-test-not-a-real-key")
    fake = FakeOpenAI(tool_called_eventually=False)
    monkeypatch.setattr(
        "daimon.adapters.cli.commands.openai.configured_transport", fake.transport_factory()
    )

    with pytest.raises(typer.Exit) as excinfo:
        openai_smoke_command(project="proj_123", model="gpt-6-luna", timeout=0.01)
    assert excinfo.value.exit_code == 1
    out = capsys.readouterr().out
    assert "smoke: FAIL" in out
    assert "timed out" in out


def test_smoke_survives_the_known_transient_content_404(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Regression check for the neutral-core F2-O skill-download 404: a single
    transient 404 on the post-publish content read must not fail the smoke
    check, since `verify_content` now retries it (see `mux/drivers/openai/skills.py`)."""
    monkeypatch.setenv("DAIMON_CHANNEL_OPENAI_KEY", "sk-test-not-a-real-key")
    fake = FakeOpenAI(content_404_once=True)
    monkeypatch.setattr(
        "daimon.adapters.cli.commands.openai.configured_transport", fake.transport_factory()
    )

    openai_smoke_command(project="proj_123", model="gpt-6-luna")

    out = capsys.readouterr().out
    assert "smoke: OK" in out
    # Agent creation and session creation each independently verify the
    # pinned skill content; the 404 only ever hits the first of those reads.
    assert fake._content_calls == 3
