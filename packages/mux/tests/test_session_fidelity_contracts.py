"""Session extension/native fields survive storage without provider types."""

import copy
import pickle

import pytest
from mux.contracts.extensions import ExtensionConfig
from mux.contracts.ids import ChannelRef, ResourceRef, Revision, ThreadRef
from mux.contracts.resources import Continuity, ProviderBinding, Session, SessionSpec

REF = ResourceRef(id="session", kind="session", provider="anthropic", account_scope_id="org")
REVISION = Revision(local=1)


def _spec() -> SessionSpec:
    return SessionSpec(
        agent=REF.model_copy(update={"id": "agent", "kind": "agent"}),
        agent_revision=REVISION,
        config_revision=0,
        extensions={
            "anthropic.session_create": ExtensionConfig(
                namespace="anthropic.session_create", version=1, value={"vault_ids": []}
            )
        },
    )


def _record() -> Session:
    return Session(
        ref=REF,
        binding=ProviderBinding(
            id="binding",
            thread=ThreadRef(
                channel=ChannelRef(tenant_id="tenant", platform="discord", channel_id="channel"),
                thread_id="thread",
            ),
            provider="anthropic",
            profile="anthropic.managed_agents",
            native_refs={"session": REF.id},
            generation=1,
            config_revision=0,
        ),
        continuity=Continuity(
            conversation="native_session", workspace="native_reuse", processes="unknown"
        ),
        requested_revision=REVISION,
        effective_revision=REVISION,
        state="idle",
        native={"id": REF.id, "future": [None, {"kept": True}]},
    )


@pytest.mark.parametrize("record", [_spec(), _record()])
def test_session_fidelity_round_trip_and_pickle(record: SessionSpec | Session) -> None:
    restored = type(record).model_validate_json(record.model_dump_json(exclude_unset=True))
    assert restored == record
    assert restored.model_fields_set == record.model_fields_set
    assert pickle.loads(pickle.dumps(record)) == record
    assert copy.deepcopy(record) == record


def test_session_extension_map_is_immutable() -> None:
    with pytest.raises(TypeError):
        _spec().extensions["new"] = ExtensionConfig(namespace="anthropic.new", version=1, value={})
