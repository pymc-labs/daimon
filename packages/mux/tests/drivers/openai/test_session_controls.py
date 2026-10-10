"""Current documented Agents controls through the real SDK, without network."""

from __future__ import annotations

import json

import httpx
import pytest
from mux.contracts.actions import UserMessage
from mux.contracts.events import TextPart
from mux.contracts.ids import Revision
from mux.contracts.resources import SessionSpec
from mux.drivers.openai import OpenAIDriver
from mux.drivers.openai.session_controls import SessionControls
from mux.drivers.openai.transport import SDKTransport
from mux.drivers.openai.turn import MemoryRecoveryJournal
from mux.drivers.openai.usage import MemoryUsageRevisions
from mux.errors import UnsupportedCapability
from openai import AsyncOpenAI
from pydantic import ValidationError

from .conftest import REF, SCOPE, native_session


@pytest.mark.parametrize(
    "value",
    [{"spend_limit_usd_cents": n} for n in (True, 0, -1, 1.5, "220")]
    + [{"container_size": "64gb"}, {"limit": 220}],
)
def test_invalid_controls_refuse_before_client_or_key_access(value: object) -> None:
    with pytest.raises(ValidationError):
        SessionControls.model_validate(value)


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [None, "small", "medium", "large"])
@pytest.mark.parametrize("limit", [None, 5, 220])
async def test_controls_use_native_fields_and_preserve_omitted_defaults(
    size: str | None, limit: int | None
) -> None:
    controls = SessionControls.model_validate(
        {"container_size": size, "spend_limit_usd_cents": limit}
    )
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/v1/agents/a":
            return httpx.Response(200, json={"id": "a", "metadata": {}})
        assert request.url.path == "/v1/agents/sessions"
        return httpx.Response(200, json=native_session())

    async with AsyncOpenAI(
        api_key="offline-placeholder",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle)),
    ) as sdk:
        driver = OpenAIDriver(
            SDKTransport(sdk),
            account_scope_id="project",
            authorization=lambda scope, kind, id_: scope == SCOPE,
            journal=MemoryRecoveryJournal(),
            usage_revisions=MemoryUsageRevisions(),
            session_controls=controls,
        )
        session = await driver.sessions.create(
            SCOPE,
            SessionSpec(
                agent=REF.model_copy(update={"kind": "agent", "id": "a"}),
                agent_revision=Revision(local=0),
                config_revision=1,
                environment=REF.model_copy(update={"kind": "environment", "id": "template"}),
            ),
            key="create",
        )
    assert session.continuity.workspace == "native_reuse"
    body = json.loads(seen[-1].content)
    expected_environment = {"type": "openai_hosted", "environment_template_id": "template"}
    if size is not None:
        expected_environment["container_size"] = size
    expected = {
        "agent_id": "a",
        "environment": expected_environment,
        "metadata": {"mux_tenant": "t", "mux_config_revision": "1"},
    }
    if limit is not None:
        expected["spend_control"] = {"limit": limit}
    assert body == expected
    assert seen[-1].headers["OpenAI-Beta"] == "agents=v1"
    assert seen[-1].headers["Idempotency-Key"] == "create"


@pytest.mark.asyncio
@pytest.mark.parametrize("hosted_size", [False, True])
async def test_conversation_only_supports_spend_limit_but_refuses_hosted_size(
    hosted_size: bool,
) -> None:
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.method == "GET":
            return httpx.Response(200, json={"id": "a", "metadata": {}})
        raw = native_session()
        raw["environment"] = {"type": "none"}
        return httpx.Response(200, json=raw)

    async with AsyncOpenAI(
        api_key="offline-placeholder",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle)),
    ) as sdk:
        driver = OpenAIDriver(
            SDKTransport(sdk),
            account_scope_id="project",
            authorization=lambda scope, kind, id_: scope == SCOPE,
            journal=MemoryRecoveryJournal(),
            usage_revisions=MemoryUsageRevisions(),
            profile_id="openai.conversation_only",
            session_controls=SessionControls(
                container_size="small" if hosted_size else None,
                spend_limit_usd_cents=5,
            ),
        )
        spec = SessionSpec(
            agent=REF.model_copy(update={"kind": "agent", "id": "a"}),
            agent_revision=Revision(local=0),
            config_revision=1,
        )
        initial = UserMessage(content=(TextPart(text="hello"),))
        if hosted_size:
            with pytest.raises(UnsupportedCapability, match="hosted_container_size"):
                await driver.sessions.create(SCOPE, spec, key="create", initial=initial)
            assert seen == []
        else:
            await driver.sessions.create(SCOPE, spec, key="create", initial=initial)
            body = json.loads(seen[-1].content)
            assert body["environment"] == {"type": "none"}
            assert body["spend_control"] == {"limit": 5}


@pytest.mark.asyncio
@pytest.mark.parametrize("actual_model", ["gpt-6-luna", "wrong-model", None])
async def test_explicit_session_model_override_is_verified(actual_model: str | None) -> None:
    from mux.errors import ContinuityLost

    seen = []

    def handle(request):
        seen.append(request)
        if request.method == "GET":
            return httpx.Response(200, json={"id": "a", "metadata": {}})
        raw = native_session()
        raw["agent"] = {"id": "a", "model": actual_model}
        return httpx.Response(200, json=raw)

    async with AsyncOpenAI(
        api_key="offline", http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle))
    ) as sdk:
        driver = OpenAIDriver(
            SDKTransport(sdk),
            account_scope_id="project",
            authorization=lambda scope, kind, id_: scope == SCOPE,
            journal=MemoryRecoveryJournal(),
            usage_revisions=MemoryUsageRevisions(),
            session_controls=SessionControls(model="gpt-6-luna"),
        )
        spec = SessionSpec(
            agent=REF.model_copy(update={"kind": "agent", "id": "a"}),
            agent_revision=Revision(local=0),
            config_revision=1,
        )
        if actual_model == "gpt-6-luna":
            await driver.sessions.create(SCOPE, spec, key="create")
        else:
            with pytest.raises(ContinuityLost, match="model"):
                await driver.sessions.create(SCOPE, spec, key="create")
    assert json.loads(seen[-1].content)["agent"] == {"model": "gpt-6-luna"}


def test_only_luna_is_an_allowed_session_model_control():
    for model in ("gpt-6-astra", "gpt-5-nano", "gpt-6-sol", ""):
        with pytest.raises(ValidationError):
            SessionControls.model_validate({"model": model})


@pytest.mark.asyncio
@pytest.mark.parametrize("native_enabled", [False, True, None])
async def test_session_override_disables_and_verifies_delegation(native_enabled):
    from mux.errors import ProviderError

    seen = []

    def handle(request):
        seen.append(request)
        if request.method == "GET":
            return httpx.Response(
                200, json={"id": "a", "metadata": {}, "multi_agent": {"enabled": False}}
            )
        raw = native_session()
        raw["agent"] = {
            "id": "a",
            "model": "gpt-6-luna",
            "multi_agent": {"enabled": native_enabled},
        }
        return httpx.Response(200, json=raw)

    async with AsyncOpenAI(
        api_key="offline",
        max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle)),
    ) as sdk:
        driver = OpenAIDriver(
            SDKTransport(sdk),
            account_scope_id="project",
            authorization=lambda scope, kind, id_: scope == SCOPE,
            journal=MemoryRecoveryJournal(),
            usage_revisions=MemoryUsageRevisions(),
            session_controls=SessionControls(model="gpt-6-luna", multi_agent_enabled=False),
        )
        spec = SessionSpec(
            agent=REF.model_copy(update={"kind": "agent", "id": "a"}),
            agent_revision=Revision(local=0),
            config_revision=1,
        )
        if native_enabled is False:
            await driver.sessions.create(SCOPE, spec, key="create")
        else:
            with pytest.raises(ProviderError) as refused:
                await driver.sessions.create(SCOPE, spec, key="create")
            assert refused.value.native_code == "host_delegation_enabled"
    assert json.loads(seen[-1].content)["agent"] == {
        "model": "gpt-6-luna",
        "multi_agent": {"enabled": False},
    }


def test_enabled_delegation_cannot_be_requested_by_host_controls():
    with pytest.raises(ValidationError):
        SessionControls.model_validate({"multi_agent_enabled": True})
