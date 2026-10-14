"""Golden HTTP capture uses the same fail-closed SDK admission as QA replays."""

from __future__ import annotations

import importlib.util
import os
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Protocol, cast

import httpx
import pytest
from anthropic import AsyncAnthropic
from daimon.testing.ma_transport import ScriptedTransport
from daimon.testing.turn_fakes import FakeEventsResource


class Capture(Protocol):
    clients: dict[int, AsyncAnthropic]
    scripts: list[ScriptedTransport]

    def assert_consumed(self) -> None: ...


class CaptureFactory(Protocol):
    def __call__(self, patch: pytest.MonkeyPatch) -> Capture: ...


SPEC = importlib.util.spec_from_file_location(
    "golden_http_capture", Path(__file__).resolve().parents[1] / "golden/http_turn.py"
)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("golden HTTP capture module missing")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
HttpTurnFixtures = cast(CaptureFactory, MODULE.HttpTurnFixtures)


class NamesOnly(Mapping[str, str]):
    def __init__(self, name: str) -> None:
        self.name = name
        self.reads = 0

    def __iter__(self) -> Iterator[str]:
        return iter((self.name,))

    def __len__(self) -> int:
        return 1

    def __getitem__(self, key: str) -> str:
        if key != self.name:
            raise KeyError(key)
        self.reads += 1
        raise AssertionError("golden fixture read an ambient credential")


@pytest.mark.parametrize(
    "name",
    (
        "ANTHROPIC_WEBHOOK_SIGNING_KEY",
        "ANTHROPIC_CUSTOM_HEADERS",
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_FUTURE_SETTING",
        "OPENAI_ADMIN_KEY",
        "GOOGLE_API_KEY",
    ),
)
async def test_golden_http_fixture_refuses_before_request_or_value_read(
    name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    environment = NamesOnly(name)
    capture = HttpTurnFixtures(monkeypatch)
    resource = FakeEventsResource()

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("golden fixture allocated HTTP before ambient refusal")

    with monkeypatch.context() as scoped:
        scoped.setattr(os, "environ", environment)
        scoped.setattr(httpx, "Client", forbidden)
        scoped.setattr(httpx, "AsyncClient", forbidden)
        with pytest.raises(ValueError, match="refuses ambient"):
            await resource.send("session_fixture", events=[])
    if environment.reads or resource.sent_events or capture.clients:
        raise AssertionError("golden fixture admitted an ambient SDK setting")
    if any(script.requests for script in capture.scripts):
        raise AssertionError("golden fixture attempted HTTP on refused admission")


async def test_golden_http_fixture_retains_real_sdk_request_shape_and_closure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(os, "environ", {})
    capture = HttpTurnFixtures(monkeypatch)
    resource = FakeEventsResource()
    events = [{"type": "user.message", "content": [{"type": "text", "text": "hello"}]}]
    try:
        await resource.send("session_fixture", events=events)
        capture.assert_consumed()
        if resource.sent_events != [("session_fixture", events)] or len(capture.scripts) != 1:
            raise AssertionError("isolated SDK changed the source fixture input")
        request = capture.scripts[0].requests[0]
        if (request.method, request.path, request.query, request.json()) != (
            "POST",
            "/v1/sessions/session_fixture/events",
            (("beta", "true"),),
            {"events": events},
        ):
            raise AssertionError("golden SDK request shape changed")
        headers = dict(request.protocol_headers)
        if headers.get("anthropic-beta") != "managed-agents-2026-04-01":
            raise AssertionError("golden SDK protocol header changed")
        for client in capture.clients.values():
            if client.max_retries != 0 or client.api_key != "offline-fixture":
                raise AssertionError("golden SDK configuration changed")
    finally:
        for client in capture.clients.values():
            await client.close()
    if any(not client.is_closed() for client in capture.clients.values()):
        raise AssertionError("golden SDK did not close its owned client")
