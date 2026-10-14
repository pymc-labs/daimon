"""Read-only, explicitly routed ASGI observations; no native turn evidence."""

from __future__ import annotations

import asyncio
import os
import re
import time
from collections.abc import AsyncIterator, Mapping
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Literal
from unittest.mock import patch
from urllib.parse import urlsplit

import httpx
from daimon.testing.isolated_sdk import build_async_anthropic
from daimon.testing.outcome_oracle import CheckResult, OutcomeReport
from daimon.testing.product_profiles import QAProductProfile, current_product_profile
from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic_settings import DotEnvSettingsSource
from starlette.types import ASGIApp

HTTP_SOURCE_PINS = {
    "QA-I7-MCP-OAUTH-DISCOVERY": "85ae51df972c745edccfa895c7310ef3bcf5e368bd6fb7db948d70addd4e316d",
    "QA-NEW24-EXPIRED-LINK-HTML-PAGE": (
        "8e3c7e0021621387a6a8bfd1ad5c04030e3b450dc506be5f0371da5c70ebf0e0"
    ),
}
type OriginName = Literal["mcp_public_url", "notebook_host", "report_host"]
type Backend = Literal["anthropic", "openai", "gemini"]


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class HTTPExpectation(Record):
    kind: Literal["http_check"]
    url: str = Field(min_length=1)
    expect_status: int | None = Field(default=None, ge=100, le=599)
    expect_content_type: str | None = Field(default=None, min_length=1)
    body_absent: str | None = None

    @model_validator(mode="after")
    def predicate(self) -> HTTPExpectation:
        if all(v is None for v in (self.expect_status, self.expect_content_type, self.body_absent)):
            raise ValueError("HTTP check needs a preserved predicate")
        if self.body_absent is not None:
            re.compile(self.body_absent)
        return self


HTTP_SOURCE_ASSERTIONS: dict[str, tuple[HTTPExpectation, ...]] = {
    "QA-I7-MCP-OAUTH-DISCOVERY": tuple(
        HTTPExpectation(
            kind="http_check",
            url="{env.mcp_public_url}/.well-known/oauth-protected-resource/mcp" + suffix,
            expect_status=200,
        )
        for suffix in ("", "/")
    ),
    "QA-NEW24-EXPIRED-LINK-HTML-PAGE": tuple(
        HTTPExpectation(
            kind="http_check",
            url=path,
            expect_content_type="text/html",
            body_absent='\\{\\"detail\\"',
        )
        for path in (
            "{env.notebook_host}/qa-missing-{nonce}/",
            "{env.report_host}/r/qa-missing-{nonce}",
        )
    ),
}


class HTTPObservation(Record):
    assertion_index: int = Field(ge=0)
    origin: OriginName
    method: Literal["GET"] = "GET"
    requested_url: str
    response_url: str
    status: int = Field(ge=100, le=599)
    headers: tuple[tuple[str, str], ...]
    body: str
    started_s: float = Field(ge=0)
    finished_s: float = Field(ge=0)

    @model_validator(mode="after")
    def measured_response(self) -> HTTPObservation:
        if self.finished_s < self.started_s or self.response_url != self.requested_url:
            raise ValueError("HTTP response lacks its actual request window/identity")
        return self


class AdminHTTPRun(Record):
    """Administrative evidence, distinct from a turn or provider execution."""

    mode: Literal["offline_admin_http"] = "offline_admin_http"
    scenario_id: str
    source_sha256: str
    backend: Backend
    assertions: tuple[HTTPExpectation, ...]
    urls: tuple[str | None, ...]
    observations: tuple[HTTPObservation, ...] = ()

    @model_validator(mode="after")
    def pinned_administration(self) -> AdminHTTPRun:
        if HTTP_SOURCE_PINS.get(self.scenario_id) != self.source_sha256:
            raise ValueError("HTTP administration source is not bound")
        if self.assertions != HTTP_SOURCE_ASSERTIONS[self.scenario_id] or len(self.urls) != len(
            self.assertions
        ):
            raise ValueError("HTTP administration must retain its complete source predicates")
        indices = [o.assertion_index for o in self.observations]
        if len(indices) != len(set(indices)) or any(i >= len(self.assertions) for i in indices):
            raise ValueError("HTTP observations repeat or exceed their source assertions")
        for observation in self.observations:
            index = observation.assertion_index
            if self.urls[index] is None or not self.assertions[index].url.startswith(
                "{env." + observation.origin + "}"
            ):
                raise ValueError("HTTP observation belongs to an unbound/foreign origin")
        return self


@dataclass(frozen=True)
class HTTPApp:
    origin: str
    app: ASGIApp
    path_prefix: str = ""


class HTTPObserver:
    """Every admitted origin has an ASGI transport; there is no network fallback."""

    def __init__(self, apps: Mapping[OriginName, HTTPApp], *, nonce: str) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", nonce):
            raise ValueError("HTTP fixture nonce is not a path component")
        self._apps = MappingProxyType(dict(apps))
        self.nonce = nonce
        for name, bound in self._apps.items():
            if bound.path_prefix not in ("", "/n") or (
                bound.path_prefix and name != "notebook_host"
            ):
                raise ValueError("HTTP app prefix is not a known notebook route")
            url = urlsplit(bound.origin)
            if (
                name not in {"mcp_public_url", "notebook_host", "report_host"}
                or url.scheme != "https"
                or not url.hostname
                or url.username is not None
                or url.password is not None
                or url.path
                or url.query
                or url.fragment
                or any(ord(c) <= 32 for c in bound.origin)
            ):
                raise ValueError("HTTP origin must be an explicit bare HTTPS authority")

    def resolve(self, template: str) -> tuple[OriginName, HTTPApp, str]:
        if template not in {e.url for source in HTTP_SOURCE_ASSERTIONS.values() for e in source}:
            raise ValueError("HTTP URL has no bound administrative source")
        for name, bound in self._apps.items():
            prefix = "{env." + name + "}"
            if template.startswith(prefix):
                tail = template[len(prefix) :].replace("{nonce}", self.nonce)
                parsed = urlsplit(tail)
                if (
                    not tail.startswith("/")
                    or tail.startswith("//")
                    or parsed.netloc
                    or parsed.scheme
                    or parsed.query
                    or parsed.fragment
                    or "{" in tail
                    or "}" in tail
                    or "\\" in tail
                    or any(ord(c) <= 32 for c in tail)
                ):
                    raise ValueError("HTTP URL is not bound to a local app path")
                return name, bound, str(httpx.URL(bound.origin + bound.path_prefix + tail))
        raise ValueError("HTTP origin has no explicit app binding")

    async def observe(self, index: int, expectation: HTTPExpectation) -> HTTPObservation:
        if type(index) is not int or index < 0:
            raise ValueError("HTTP assertion index must be nonnegative")
        name, bound, url = self.resolve(expectation.url)
        started = time.monotonic()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=bound.app),
            trust_env=False,
            follow_redirects=False,
            timeout=5,
        ) as client:
            response = await asyncio.wait_for(client.get(url), timeout=5)
            if len(response.content) > 1024 * 1024:
                raise ValueError("HTTP response exceeds the offline observation bound")
            return HTTPObservation(
                assertion_index=index,
                origin=name,
                requested_url=str(response.request.url),
                response_url=str(response.url),
                status=response.status_code,
                headers=tuple(response.headers.multi_items()),
                body=response.text,
                started_s=started,
                finished_s=time.monotonic(),
            )


def http_check(
    observation: HTTPObservation | None, expectation: HTTPExpectation, *, url: str
) -> CheckResult:
    if observation is None:
        return CheckResult(kind="http_check", turn=None, status="PENDING", code="HTTP_NOT_OBSERVED")
    if observation.requested_url != url or observation.response_url != url:
        return CheckResult(
            kind="http_check", turn=None, status="PENDING", code="HTTP_IDENTITY_UNBOUND"
        )
    matched = expectation.expect_status is None or observation.status == expectation.expect_status
    if expectation.expect_content_type is not None:
        values = [
            v.split(";", 1)[0].strip().lower()
            for k, v in observation.headers
            if k.lower() == "content-type"
        ]
        matched = matched and values == [expectation.expect_content_type.lower()]
    if expectation.body_absent is not None:
        matched = matched and re.search(expectation.body_absent, observation.body) is None
    return CheckResult(
        kind="http_check",
        turn=None,
        status="PASS" if matched else "FAIL",
        code="HTTP_EXPECTATIONS_MET" if matched else "HTTP_EXPECTATIONS_MISMATCH",
        evidence_ids=(f"http[{observation.assertion_index}]",),
    )


def http_outcome(run: AdminHTTPRun) -> OutcomeReport:
    observations = {o.assertion_index: o for o in run.observations}
    checks: list[CheckResult] = []
    for index, (expectation, url) in enumerate(zip(run.assertions, run.urls, strict=True)):
        if url is None:
            checks.append(
                CheckResult(
                    kind="http_check", turn=None, status="PENDING", code="HTTP_ORIGIN_UNBOUND"
                )
            )
        else:
            checks.append(http_check(observations.get(index), expectation, url=url))
    return OutcomeReport(scenario_id=run.scenario_id, backend=run.backend, checks=tuple(checks))


@asynccontextmanager
async def offline_http_apps(
    root: Path, *, nonce: str, profile: QAProductProfile | None = None
) -> AsyncIterator[HTTPObserver]:
    """Actual app factories and disposable link stores; no sockets or native turns.

    SDK admission happens before the synchronous settings phase. Replacing the
    environment mapping during that phase prevents BaseSettings from copying
    operator values even for explicitly supplied settings. No values are saved
    or cleared; the original mapping reference is restored before any await.
    """

    def refused(request: httpx.Request) -> httpx.Response:
        raise AssertionError("administrative HTTP fixture attempted unbound outbound I/O")

    if profile is not None:
        if profile != current_product_profile():
            raise ValueError("HTTP factory differs from active product profile")
        profile.require_available()
    if root.exists() and any(root.iterdir()):
        raise ValueError("HTTP fixtures require an empty disposable directory")
    async with AsyncExitStack() as resources:
        sdk = await resources.enter_async_context(
            build_async_anthropic(
                transport=httpx.MockTransport(refused),
                api_key="offline-placeholder",
                base_url="https://offline.invalid",
                headers={},
            )
        )
        root.mkdir(parents=True, exist_ok=True)
        with (
            patch.object(os, "environ", {}),
            # FastMCP also constructs settings at cold import. Keep that
            # dependency's dotenv source empty without opening any key file.
            patch.object(DotEnvSettingsSource, "_read_env_files", return_value={}),
        ):
            from daimon.adapters.mcp.server import create_mcp_app
            from daimon.core.billing import BillingConfig
            from daimon.core.config import (
                AnthropicSettings,
                DatabaseSettings,
                DiscordSettings,
                McpSettings,
                Settings,
            )
            from notebook_host.config import Settings as NotebookSettings
            from notebook_host.main import create_app as create_notebook
            from pydantic import HttpUrl, PostgresDsn, SecretStr
            from report_host import reports_store
            from report_host.config import Settings as ReportSettings
            from report_host.main import create_app as create_report
            from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

            settings = Settings(
                _env_file=None,  # pyright: ignore[reportCallIssue] -- BaseSettings admission argument
                database=DatabaseSettings(
                    url=PostgresDsn(
                        "postgresql+asyncpg://unused:unused@localhost/daimon_test_http_unused"
                    )
                ),
                anthropic=AnthropicSettings(api_key=SecretStr("offline-placeholder")),
                mcp=McpSettings(
                    jwt_secret=SecretStr("offline-public-http-signing-only-123456"),
                    public_url=HttpUrl("https://qa-mcp.invalid/mcp"),
                ),
            )
            if profile is not None:
                settings = profile.apply(
                    settings.model_copy(
                        update={
                            "discord": DiscordSettings(
                                bot_token=SecretStr("offline-discord-unused")
                            )
                        }
                    ),
                    service="core",
                )
            mcp = create_mcp_app(
                settings=settings,
                sessionmaker=async_sessionmaker[AsyncSession](expire_on_commit=False),
                anthropic=sdk,
                billing_config=BillingConfig(
                    secret_key=SecretStr("offline-stripe-unused"),
                    webhook_secret=SecretStr("offline-webhook-unused"),
                    prices={},
                    success_url="https://qa-mcp.invalid/success",
                    cancel_url="https://qa-mcp.invalid/cancel",
                ),
            )
            notebook_settings = NotebookSettings(
                _env_file=None,  # pyright: ignore[reportCallIssue] -- BaseSettings admission argument
                data_dir=root / "notebooks",
                admin_secret=SecretStr("offline-admin-only"),
                public_url_base="https://qa-notebook.invalid/n",
            )
            if profile is not None:
                notebook_settings = profile.apply(notebook_settings, service="notebook")
            notebook = create_notebook(notebook_settings)
            report_settings = ReportSettings(
                _env_file=None,  # pyright: ignore[reportCallIssue] -- BaseSettings admission argument
                data_dir=root / "reports",
                admin_secrets=[SecretStr("offline-admin-only")],
                mcp_url=HttpUrl("https://qa-mcp.invalid/mcp"),
                public_url_base=HttpUrl("https://qa-report.invalid"),
            )
            if profile is not None:
                report_settings = profile.apply(report_settings, service="report")
            conn = reports_store.connect(report_settings.data_dir)
            resources.callback(conn.close)
            outbound = httpx.AsyncClient(transport=httpx.MockTransport(refused), trust_env=False)
            resources.push_async_callback(outbound.aclose)
            with (
                patch.object(reports_store, "connect", return_value=conn),
                patch("report_host.main.httpx.AsyncClient", return_value=outbound),
            ):
                report = create_report(report_settings)
        # Only read-only public paths are bound; no lifecycle/subprocess boot is
        # needed for unknown links. The real report store and clients close above.
        yield HTTPObserver(
            {
                "mcp_public_url": HTTPApp("https://qa-mcp.invalid", mcp),
                "notebook_host": HTTPApp("https://qa-notebook.invalid", notebook, path_prefix="/n"),
                "report_host": HTTPApp("https://qa-report.invalid", report),
            },
            nonce=nonce,
        )
