"""Offline Gemini native-catalog wire fake and builders shared by mux and host test suites."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from copy import deepcopy
from dataclasses import dataclass
from typing import TYPE_CHECKING

import httpx
import pytest_asyncio
from pydantic import TypeAdapter

from mux.contracts.ids import ModelRef, Scope, SkillRef
from mux.contracts.resources import AgentSpec, SkillUpload, SkillUploadFile, ToolSpec
from mux.drivers.gemini import GeminiManagedAgents
from mux.drivers.gemini.catalog_transport import SDKAgentCatalogTransport
from mux.drivers.gemini.transport import Object
from mux.state.memory import MemoryStateStore
from mux.testing.drivers.gemini.fake import FakeTransport, MemoryStorage
from mux.testing.isolated_sdk import build_gemini_client

if TYPE_CHECKING:
    from google.genai import Client

SCOPE = Scope(tenant_id="tenant", account_id="account", principal_id="admin", authorization_id="a1")
WORKSPACE = "native-a1-workspace"
_OBJECT: TypeAdapter[Object] = TypeAdapter(Object)
TLS_NAMES = frozenset(
    {
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "SSLKEYLOGFILE",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
        "REQUESTS_CA_BUNDLE",
        "CURL_CA_BUNDLE",
    }
)


def replay_client(transport: httpx.MockTransport) -> Client:
    # Refuse present names before values/import/allocation, including TLS keylog.
    if any(
        name.upper() in TLS_NAMES
        or name.upper().startswith(("ANTHROPIC_", "OPENAI_", "GOOGLE_", "GEMINI_"))
        for name in os.environ
    ):
        raise ValueError("A1 replay refuses ambient SDK configuration")
    return build_gemini_client(transport=transport)


def spec(*, name: str = "source", pins: tuple[SkillRef, ...] = ()) -> AgentSpec:
    return AgentSpec(
        name=name,
        description="qa-original",
        model=ModelRef(provider="gemini", id="gemini-3.8-flash"),
        system="Help this workspace.",
        tools=(ToolSpec(name="code_execution", kind="builtin"),),
        skills=pins,
        metadata={"daimon.name": name},
    )


class Wire:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.agents: dict[str, Object] = {}
        self.get_changes: Object = {}
        self.lose_post = False
        self.sparse_ack = False
        self.omit_empty_lists = False

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.method == "POST" and request.url.path == "/v1beta/agents":
            body = _OBJECT.validate_json(request.content, strict=True)
            id_ = body["id"]
            if not isinstance(id_, str):
                raise ValueError("missing authored native identity")
            self.agents[id_] = deepcopy(body)
            if self.lose_post:
                self.lose_post = False
                raise httpx.ReadError("offline accepted POST response lost", request=request)
            response = (
                {"id": id_, "tools": None, "base_environment": None} if self.sparse_ack else body
            )
            return httpx.Response(200, json=response)
        if request.method == "GET" and request.url.path.startswith("/v1beta/agents/"):
            id_ = request.url.path.rsplit("/", 1)[-1]
            body = self.agents.get(id_)
            if body is None:
                return httpx.Response(404, json={"error": {"message": "not found"}})
            readback = {**deepcopy(body), **self.get_changes}
            if self.omit_empty_lists:
                if readback.get("tools") == []:
                    del readback["tools"]
                environment = readback.get("base_environment")
                if isinstance(environment, dict) and environment.get("sources") == []:
                    del environment["sources"]
            return httpx.Response(200, json=readback)
        raise ValueError("unexpected native catalog request")


@dataclass
class Case:
    wire: Wire
    storage: MemoryStorage
    interactions: FakeTransport
    native: SDKAgentCatalogTransport
    driver: GeminiManagedAgents


@pytest_asyncio.fixture
async def case() -> AsyncIterator[Case]:
    wire, storage, interactions = Wire(), MemoryStorage(), FakeTransport()
    client = replay_client(httpx.MockTransport(wire.handle))
    try:
        native = SDKAgentCatalogTransport(client)
        driver = GeminiManagedAgents(
            interactions,
            storage=storage,
            state_store=MemoryStateStore(),
            account_scope_id=WORKSPACE,
            resource_catalog_enabled=True,
            resource_catalog_transport=native,
        )
        yield Case(wire, storage, interactions, native, driver)
    finally:
        await client.aio.aclose()
        client.close()


def bundle(body: bytes = b"# Original\nKeep every byte.\n") -> SkillUpload:
    return SkillUpload(
        files=(
            SkillUploadFile(path="SKILL.md", content=body),
            SkillUploadFile(path="scripts/check.sh", content=b"true\n"),
        )
    )
