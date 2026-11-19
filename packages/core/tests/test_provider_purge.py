"""Erasure deletes each provider session under the key it was created with."""

import uuid

import pytest
from daimon.core import provider_purge
from daimon.core.stores.mux_erasure import ProviderSession
from daimon.core.turn import openai_deployment


async def test_each_session_is_deleted_with_its_own_key_and_project(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant = uuid.uuid4()
    asked: list[tuple[str, str, str]] = []

    def resolve(tenant_id: str, profile: str, account_scope_id: str) -> None:
        asked.append((tenant_id, profile, account_scope_id))

    monkeypatch.setattr(openai_deployment, "usage_transport", resolve)
    sessions = [
        ProviderSession(tenant, "openai", "s-1", "proj-own", "openai.agents_sdk"),
        ProviderSession(tenant, "openai", "s-2", "default", "openai.responses"),
        ProviderSession(tenant, "gemini", "s-3", "g", "gemini.interactions"),
    ]

    assert await provider_purge.delete_provider_sessions(sessions) == 0, "no key resolves here"
    assert asked == [
        (str(tenant), "openai.agents_sdk", "proj-own"),
        (str(tenant), "openai.responses", "default"),
    ], "each OpenAI session resolves its own operator row, never the bare default key"
