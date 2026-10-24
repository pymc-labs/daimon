"""Verified native Gemini definitions dispatch by ID after a restart over SQLite storage.

The mux suite proves the same flow over `MemoryStorage`; this variant reopens the host's
`SQLiteGeminiStorage` file after the simulated restart.
"""

import json

import httpx
import pytest
from daimon.core.turn.gemini_state import SQLiteGeminiStorage
from mux.contracts.actions import UserMessage
from mux.contracts.events import TextPart
from mux.contracts.extensions import ExtensionConfig
from mux.contracts.ids import ChannelRef, ModelRef, ThreadRef
from mux.contracts.resources import SessionSpec
from mux.drivers.gemini import GeminiManagedAgents
from mux.drivers.gemini.deployment import configured_catalog_transport, configured_transport
from mux.drivers.gemini.flash import FLASH_PRIMARY, FlashTransport
from mux.errors import UnsupportedCapability
from mux.state.memory import MemoryStateStore
from mux.testing.drivers.gemini.accounting import Accounting
from mux.testing.drivers.gemini.catalog_wire import SCOPE, WORKSPACE, Wire, spec
from mux.testing.drivers.gemini.owned_clients import owned_clients


@pytest.mark.parametrize("accepted", [False, True])
@pytest.mark.parametrize("native_status", [200, 503])
async def test_actual_sdk_dispatch_keeps_verified_native_id_model_and_single_attempt(
    accepted, native_status, monkeypatch, tmp_path
):
    wire = Wire()
    posts = []

    def handle(request):
        if request.method == "GET" and request.url.path.startswith("/v1beta/interactions/"):
            return httpx.Response(
                200,
                json={
                    "id": request.url.path.rsplit("/", 1)[-1],
                    "status": "completed",
                    "environment_id": "workspace",
                    "outputs": [],
                    "usage": {
                        "total_input_tokens": 12,
                        "total_output_tokens": 3,
                        "total_thought_tokens": 0,
                    },
                },
            )
        if request.url.path == "/v1beta/interactions" and request.method == "POST":
            posts.append(json.loads(request.content))
            if native_status == 503:
                return httpx.Response(503, json={"error": {"message": "overloaded"}})
            return httpx.Response(
                200,
                json={
                    "id": f"interaction-{len(posts)}",
                    "environment_id": "workspace",
                    "status": "completed",
                    "outputs": [],
                    "usage": {
                        "total_input_tokens": 12,
                        "total_output_tokens": 3,
                        "total_thought_tokens": 0,
                    },
                },
            )
        return wire.handle(request)

    owned = owned_clients(monkeypatch, handle)
    storage = SQLiteGeminiStorage(
        tmp_path / "agents.sqlite", namespace="explicit-scoped-native-intent"
    )
    store = MemoryStateStore()
    base = configured_transport(api_key="offline-selected")
    try:
        catalog = GeminiManagedAgents(
            base,
            storage=storage,
            state_store=store,
            account_scope_id=WORKSPACE,
            resource_catalog_enabled=True,
            resource_catalog_transport=configured_catalog_transport(api_key="offline-selected"),
        )
        retained = await catalog.agents.create(SCOPE, spec(), key="owned-counterpart")
        verified = await catalog.agents.retrieve(SCOPE, retained.ref)
        assert verified == retained
        accounting = Accounting()
        policy = FlashTransport(base, accounting, native_agent=verified if accepted else None)
        backend = GeminiManagedAgents(
            policy,
            storage=storage,
            state_store=store,
            account_scope_id=WORKSPACE,
            native_agent=verified if accepted else None,
            model_for_interaction=policy.model_for_interaction,
        )
        plan = SessionSpec(
            agent=retained.ref,
            agent_revision=retained.revision,
            config_revision=1,
            extensions={
                "gemini.session": ExtensionConfig(
                    namespace="gemini.session",
                    version=1,
                    value={
                        "binding_id": "binding",
                        "thread": ThreadRef(
                            channel=ChannelRef(
                                tenant_id=SCOPE.tenant_id, platform="slack", channel_id="channel"
                            ),
                            thread_id="thread",
                        ).model_dump(mode="json"),
                    },
                )
            },
        )
        if not accepted:
            with pytest.raises(UnsupportedCapability, match="named_agent_session_plan"):
                await backend.sessions.create(SCOPE, plan, key="session")
            assert not posts and not accounting.held
            return
        session = await backend.sessions.create(SCOPE, plan, key="session")
        assert session.binding.native_refs["agent"] == retained.ref.id
        receipt = await backend.events.send(
            SCOPE, session.ref, (UserMessage(content=(TextPart(text="hello"),)),), key="send"
        )
        assert len(posts) == 1
        assert posts[0]["agent"] == retained.ref.id
        assert posts[0]["agent_config"]["model"] == FLASH_PRIMARY
        assert posts[0]["tools"] == [{"type": "code_execution"}]
        assert accounting.held == [ModelRef(provider="gemini", id=FLASH_PRIMARY)]
        assert len(accounting.receipts) == 1
        assert accounting.receipts[0].outcome == ("accepted" if native_status == 200 else "refused")
        assert receipt.status == ("queued" if native_status == 200 else "outcome_unknown")
        if native_status == 200:
            # A new call retains the same named definition and verified tariff.
            accounting.held.clear()
            # Reconstruct the ordinary driver with the persisted named definition.
            retained_storage = SQLiteGeminiStorage(
                tmp_path / "agents.sqlite", namespace="explicit-scoped-native-intent"
            )
            restarted_policy = FlashTransport(base, accounting, native_agent=verified)
            backend = GeminiManagedAgents(
                restarted_policy,
                storage=retained_storage,
                state_store=store,
                account_scope_id=WORKSPACE,
                native_agent=verified,
                model_for_interaction=restarted_policy.model_for_interaction,
            )
            await backend.events.send(
                SCOPE, session.ref, (UserMessage(content=(TextPart(text="continue"),)),), key="next"
            )
            assert len(posts) == 2
            assert posts[1]["agent"] == retained.ref.id
            assert posts[1]["agent_config"] == posts[0]["agent_config"]
            assert posts[1]["previous_interaction_id"] == "interaction-1"
            assert posts[1]["environment"] == "workspace"
            assert accounting.held == [ModelRef(provider="gemini", id=FLASH_PRIMARY)]
            assert restarted_policy.model_for_interaction("interaction-2") == ModelRef(
                provider="gemini", id=FLASH_PRIMARY
            )
    finally:
        assert all(client.is_closed for client in (*owned.synchronous, *owned.asynchronous))
