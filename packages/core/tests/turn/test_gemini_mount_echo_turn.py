"""A GCS-mounted Gemini turn reads its own completed result (F2-G r12).

On completion Gemini echoes the request's storage-mount transform. The real
SDK transport scrubs that echo, so the turn ends with its answer and the
token reaches no journal, state or log.
"""

import asyncio
import json
from datetime import UTC, datetime

import httpx
import pytest
from daimon.core.mux_backend import TurnBackendRequest
from daimon.core.turn.driver import run_turn
from daimon.core.turn.posture import BillingExempt
from daimon.testing.turn_fakes import RecordingLifecycle
from google import genai
from google.genai.types import HttpOptions, HttpRetryOptions
from mux.contracts.config import BackendConfig, ConfigRevision, resolve_default
from mux.contracts.extensions import ExtensionConfig
from mux.contracts.ids import ModelRef, PageRequest
from mux.contracts.resources import AgentSpec, EnvironmentSpec, MCPConnection, SessionSpec
from mux.drivers.gemini import GeminiManagedAgents
from mux.drivers.gemini.mcp_auth import RedactedCredential
from mux.drivers.gemini.transport import SDKTransport
from mux.state.memory import MemoryStateStore
from mux.testing.drivers.anthropic.ma_transport import ScriptedTransport
from mux.testing.drivers.gemini.fake import MemoryStorage

from .test_gemini_codec import CHANNEL, PROFILE, SCOPE, THREAD
from .test_gemini_resume import ANSWER, steps

TOKEN = RedactedCredential("offline-mount-token-never-escapes")


def snapshot(interaction_id: str, status: str, transform: object = None) -> dict:
    stamp = datetime.now(UTC).isoformat()
    raw: dict = {
        "id": interaction_id,
        "status": status,
        "created": stamp,
        "updated": stamp,
        "environment_id": "workspace",
        "steps": [],
        "usage": {"total_input_tokens": 640, "total_output_tokens": 9},
    }
    if status != "in_progress":
        raw["steps"] = steps()
        # What Gemini echoes on completion: the request's environment, mount
        # transform included (really as a list of header objects, F2-G r13).
        raw["environment"] = {
            "type": "remote",
            "sources": [{"type": "gcs", "source": "gs://skills/v1/", "target": ".agents/skills/s"}],
            "network": {
                "allowlist": [
                    {
                        "domain": "storage.googleapis.com",
                        "transform": transform,
                    }
                ]
            },
        }
    return raw


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "transform",
    [
        pytest.param([{"Authorization": "Bearer " + TOKEN}], id="real-list-shape"),
        pytest.param({"Authorization": "Bearer " + TOKEN}, id="object-shape"),
    ],
)
async def test_completed_mounted_turn_delivers_its_answer_without_the_token(
    transform: object,
    caplog: pytest.LogCaptureFixture,
) -> None:
    created: list[str] = []
    phase = {"done": False}
    caplog.set_level("DEBUG")

    def respond(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            created.append(request.url.path)
            return httpx.Response(200, json=snapshot("r12", "in_progress"))
        if "stream" in str(request.url) or "text/event-stream" in request.headers.get("accept", ""):
            phase["done"] = True
            frame = {
                "event_type": "interaction.completed",
                "interaction": snapshot("r12", "completed", transform),
            }
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=("data: " + json.dumps(frame) + "\n\n").encode(),
            )
        return httpx.Response(
            200, json=snapshot("r12", "completed" if phase["done"] else "in_progress", transform)
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        client = genai.Client(
            api_key="offline",
            http_options=HttpOptions(
                httpx_async_client=http, retry_options=HttpRetryOptions(attempts=1)
            ),
        )
        sdk = SDKTransport(client, protect_storage_credentials=True, storage_tokens={TOKEN})
        backend = GeminiManagedAgents(
            sdk, storage=MemoryStorage(), state_store=MemoryStateStore(), account_scope_id="project"
        )
        agent = await backend.agents.create(
            SCOPE,
            AgentSpec(
                name="r12",
                model=ModelRef(provider="gemini", id="gemini-3.8-flash"),
                mcp_servers=(MCPConnection(name="daimon-mcp", url="https://daimon.invalid/mcp"),),
            ),
            key="a",
        )
        env = await backend.environments.create(SCOPE, EnvironmentSpec(name="e"), key="e")
        session = await backend.sessions.create(
            SCOPE,
            SessionSpec(
                agent=agent.ref,
                agent_revision=agent.revision,
                environment=env.ref,
                config_revision=1,
                extensions={
                    "gemini.session": ExtensionConfig(
                        namespace="gemini.session",
                        version=1,
                        value={"binding_id": "b", "thread": THREAD.model_dump(mode="json")},
                    )
                },
            ),
            key="s",
        )
        revision = ConfigRevision.create(
            CHANNEL,
            1,
            resolve_default(
                BackendConfig(backend="gemini", profile=PROFILE, model="gemini-3.8-flash")
            ),
        )
        lifecycle = RecordingLifecycle()
        async with ScriptedTransport().client() as anthropic:
            state = await run_turn(
                anthropic=anthropic,
                session_id=session.ref.id,
                user_message="q",
                lifecycle=lifecycle,
                cancel=asyncio.Event(),
                billing=BillingExempt(reason="headless-unrecorded"),
                path="mux",
                scope=SCOPE,
                backend=backend,
                session_ref=session.ref,
                profile=PROFILE,
                backend_request=TurnBackendRequest(
                    profile=PROFILE,
                    client=anthropic,
                    scope=SCOPE,
                    session_id=session.ref.id,
                    session=session.ref,
                    config=revision,
                    read_timeout_s=1,
                ),
                stream_read_timeout_s=1,
            )
        journal = await backend.events.list(SCOPE, session.ref, page=PageRequest(limit=1000))
        await client.aio.aclose()
    assert state.error is None and len(lifecycle.terminal_success) == 1
    assert [c.text for c in state.content if c.kind == "text"] == [ANSWER]
    assert len(created) == 1  # never re-sent
    blob = json.dumps([e.model_dump(mode="json") for e in journal.data]) + repr(state) + caplog.text
    assert TOKEN not in blob
