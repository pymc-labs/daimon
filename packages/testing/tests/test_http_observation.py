"""Administrative HTTP observations cannot masquerade as host turns."""

from __future__ import annotations

import os
from collections.abc import Iterator, Mapping
from pathlib import Path

import httpx
import pytest
from daimon.testing.http_observation import (
    HTTP_SOURCE_ASSERTIONS,
    HTTP_SOURCE_PINS,
    AdminHTTPRun,
    HTTPApp,
    HTTPExpectation,
    HTTPObservation,
    HTTPObserver,
    http_check,
    http_outcome,
    offline_http_apps,
)
from pydantic import ValidationError
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

I7 = "QA-I7-MCP-OAUTH-DISCOVERY"
LINK = "QA-NEW24-EXPIRED-LINK-HTML-PAGE"


def response(
    *, status: int = 200, media: str = "text/html", body: str = "<html>missing</html>"
) -> HTTPApp:
    async def serve(request: Request) -> Response:
        return Response(body, status_code=status, media_type=media)

    return HTTPApp("https://bound.invalid", Starlette(routes=[Route("/{path:path}", serve)]))


def observed(
    *, url: str, index: int = 0, origin: str = "mcp_public_url", status: int = 200
) -> HTTPObservation:
    return HTTPObservation.model_validate(
        {
            "assertion_index": index,
            "origin": origin,
            "requested_url": url,
            "response_url": url,
            "status": status,
            "headers": [["content-type", "text/html; charset=utf-8"]],
            "body": "<html>missing</html>",
            "started_s": 1,
            "finished_s": 2,
        }
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(("status", "expected"), ((200, "PASS"), (404, "FAIL")))
async def test_status_predicate_uses_actual_bound_asgi_response(status: int, expected: str) -> None:
    observer = HTTPObserver({"mcp_public_url": response(status=status)}, nonce="source")
    expectation = HTTP_SOURCE_ASSERTIONS[I7][0]
    observation = await observer.observe(0, expectation)
    _, _, url = observer.resolve(expectation.url)
    if (
        observation.status != status
        or http_check(observation, expectation, url=url).status != expected
    ):
        raise AssertionError("HTTP status was not judged from the real response")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("media", "body", "expected"),
    (
        ("text/html", "<html>missing</html>", "PASS"),
        ("application/json", '{"detail":"missing"}', "FAIL"),
        ("text/html", '{"detail":"missing"}', "FAIL"),
    ),
)
async def test_content_type_and_body_are_separate_actual_predicates(
    media: str, body: str, expected: str
) -> None:
    observer = HTTPObserver(
        {"report_host": response(status=403, media=media, body=body)}, nonce="source"
    )
    expectation = HTTPExpectation(
        kind="http_check",
        url=HTTP_SOURCE_ASSERTIONS[LINK][1].url,
        expect_content_type="text/html",
        body_absent=r'\{"detail"',
    )
    observation = await observer.observe(1, expectation)
    _, _, url = observer.resolve(expectation.url)
    if http_check(observation, expectation, url=url).status != expected:
        raise AssertionError("HTTP body/type predicate was not evaluated")


@pytest.mark.parametrize(
    "url",
    (
        "https://foreign.invalid/admin",
        "{env.unknown}/admin",
        "{env.report_host}//foreign.invalid",
        "{env.report_host}/r/../admin",
        "{env.report_host}/r/qa-missing-{nonce}?token=x",
        "{env.report_host}/r/qa-missing-{nonce}#secret",
        "{env.report_host}/admin",
    ),
)
def test_unowned_templates_refuse_before_transport(url: str) -> None:
    observer = HTTPObserver({"report_host": response()}, nonce="source")
    with pytest.raises(ValueError, match="bound administrative source"):
        observer.resolve(url)


@pytest.mark.parametrize(
    "origin",
    (
        "http://foreign.invalid",
        "https://user:password@foreign.invalid",
        "https://foreign.invalid?token=x",
        "https://foreign.invalid#secret",
        "https://foreign.invalid/extra",
        "https://foreign.invalid\n",
    ),
)
def test_credential_bearing_or_non_origin_binding_is_refused(origin: str) -> None:
    with pytest.raises(ValueError, match="bare HTTPS authority"):
        HTTPObserver({"report_host": HTTPApp(origin, response().app)}, nonce="source")


def test_empty_capture_retains_every_check_as_pending_after_json_roundtrip() -> None:
    run = AdminHTTPRun(
        scenario_id=I7,
        source_sha256=HTTP_SOURCE_PINS[I7],
        backend="openai",
        assertions=HTTP_SOURCE_ASSERTIONS[I7],
        urls=(None, None),
    )
    restored = AdminHTTPRun.model_validate_json(run.model_dump_json())
    outcome = http_outcome(restored)
    if len(outcome.checks) != 2 or outcome.status != "PENDING":
        raise AssertionError("empty admin report earned a PASS")


def test_foreign_response_cannot_satisfy_a_current_request() -> None:
    observer = HTTPObserver({"mcp_public_url": response()}, nonce="source")
    urls = tuple(observer.resolve(e.url)[2] for e in HTTP_SOURCE_ASSERTIONS[I7])
    run = AdminHTTPRun(
        scenario_id=I7,
        source_sha256=HTTP_SOURCE_PINS[I7],
        backend="gemini",
        assertions=HTTP_SOURCE_ASSERTIONS[I7],
        urls=urls,
        observations=(observed(url="https://foreign.invalid/wrong"),),
    )
    outcome = http_outcome(AdminHTTPRun.model_validate_json(run.model_dump_json()))
    if outcome.status != "PENDING" or outcome.checks[0].code != "HTTP_IDENTITY_UNBOUND":
        raise AssertionError("foreign current HTTP response earned a PASS")


@pytest.mark.parametrize(
    "fault",
    ("empty_checks", "foreign_source", "duplicate_response", "foreign_origin", "backward_window"),
)
def test_admin_envelope_refuses_changed_source_and_response_lineage(fault: str) -> None:
    observer = HTTPObserver({"mcp_public_url": response()}, nonce="source")
    urls = tuple(observer.resolve(e.url)[2] for e in HTTP_SOURCE_ASSERTIONS[I7])
    run = AdminHTTPRun(
        scenario_id=I7,
        source_sha256=HTTP_SOURCE_PINS[I7],
        backend="anthropic",
        assertions=HTTP_SOURCE_ASSERTIONS[I7],
        urls=urls,
        observations=(observed(url=urls[0]),),
    )
    raw = run.model_dump(mode="json")
    if fault == "empty_checks":
        raw["assertions"] = []
    elif fault == "foreign_source":
        raw["source_sha256"] = "0" * 64
    elif fault == "duplicate_response":
        raw["observations"] = [run.observations[0].model_dump(mode="json")] * 2
    elif fault == "foreign_origin":
        raw["observations"] = [
            run.observations[0].model_dump(mode="json") | {"origin": "report_host"}
        ]
    else:
        raw["observations"] = [run.observations[0].model_dump(mode="json") | {"finished_s": 0}]
    with pytest.raises(ValidationError):
        AdminHTTPRun.model_validate(raw)


class NamesOnly(Mapping[str, str]):
    def __init__(self, key: str = "ANTHROPIC_FUTURE_HTTP_SETTING") -> None:
        self.key = key

    def __iter__(self) -> Iterator[str]:
        return iter((self.key,))

    def __len__(self) -> int:
        return 1

    def __getitem__(self, key: str) -> str:
        if key != self.key:
            raise KeyError(key)
        raise AssertionError("HTTP app preparation read an SDK environment value")


@pytest.mark.asyncio
async def test_app_settings_phase_cannot_mask_hostile_sdk_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("HTTP fixture allocated HTTP before ambient refusal")

    with monkeypatch.context() as scoped:
        scoped.setattr(os, "environ", NamesOnly())
        scoped.setattr(httpx, "Client", forbidden)
        scoped.setattr(httpx, "AsyncClient", forbidden)
        with pytest.raises(ValueError, match="refuses ambient"):
            async with offline_http_apps(tmp_path, nonce="source"):
                raise AssertionError("settings phase bypassed the SDK refusal")
    if list(tmp_path.iterdir()):
        raise AssertionError("ambient refusal wrote fixture data")


@pytest.mark.asyncio
async def test_app_fixture_does_not_reuse_an_existing_store(tmp_path: Path) -> None:
    marker = tmp_path / "operator-data"
    marker.write_text("preserve")
    with pytest.raises(ValueError, match="empty disposable"):
        async with offline_http_apps(tmp_path, nonce="source"):
            raise AssertionError("fixture reused an existing store")
    if marker.read_text() != "preserve":
        raise AssertionError("fixture modified existing data")


@pytest.mark.asyncio
async def test_app_settings_never_copy_operator_daemon_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with monkeypatch.context() as scoped:
        scoped.setattr(os, "environ", NamesOnly("DAIMON_ANTHROPIC__API_KEY"))
        async with offline_http_apps(tmp_path, nonce="settings") as observer:
            observation = await observer.observe(0, HTTP_SOURCE_ASSERTIONS[I7][0])
            if observation.status != 404:
                raise AssertionError("synthetic app configuration changed")
