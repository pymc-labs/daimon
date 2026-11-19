"""Erasure deletes each provider session under the key it was created with."""

import uuid
from collections.abc import Mapping

import pytest
from daimon.core import provider_purge
from daimon.core.stores.mux_erasure import ProviderSession
from daimon.core.turn import openai_deployment


class _Upstream:
    """Answers the session delete: one idle read, then the DELETE."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def request(
        self,
        method: str,
        path: str,
        *,
        body: object = None,
        query: Mapping[str, str | int] | None = None,
        key: str | None = None,
    ) -> dict[str, object]:
        self.calls.append((method, path))
        return {"id": "s-1", "status": "idle", "vault_ids": []} if method == "GET" else {}


async def test_each_session_is_deleted_with_its_own_key_and_project(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant = uuid.uuid4()
    own = _Upstream()
    asked: list[tuple[str, str, str]] = []

    def resolve(tenant_id: str, profile: str, account_scope_id: str) -> _Upstream | None:
        asked.append((tenant_id, profile, account_scope_id))
        return own if account_scope_id == "proj-own" else None

    monkeypatch.setattr(openai_deployment, "usage_transport", resolve)
    sessions = [
        ProviderSession(tenant, "openai", "s-1", "proj-own", "openai.persistent_workspace"),
        ProviderSession(tenant, "openai", "s-2", "default", "openai.conversation_only"),
        ProviderSession(tenant, "gemini", "s-3", "g", "gemini.interactions"),
    ]

    assert await provider_purge.delete_provider_sessions(sessions) == 1
    assert asked == [
        (str(tenant), "openai.persistent_workspace", "proj-own"),
        (str(tenant), "openai.conversation_only", "default"),
    ], "each OpenAI session resolves its own operator row, never the bare default key"
    assert own.calls == [
        ("GET", "/agents/sessions/s-1"),
        ("DELETE", "/agents/sessions/s-1"),
    ], "deleted on the transport its own key resolved, under the tenant's scope"
