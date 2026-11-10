"""Stored config revisions and session plans keep their exact digests.

A new field on a digested contract must be declared `additive()` (absent while at
its default), or these golden digests change and the stored ones stop matching.
"""

from __future__ import annotations

from mux.contracts._base import Contract
from mux.contracts.config import ResolvedBackend
from mux.contracts.digest import additive, digest_body
from mux.contracts.ids import ResourceRef, Revision
from mux.contracts.resources import ResourceBinding, SessionSpec
from mux.drivers.openai.session_controls import SessionControls
from mux.state.operations import request_digest

REF = ResourceRef(
    id="agent-1", kind="agent", provider="openai", account_scope_id="proj", tenant_id="t"
)
SPEC = SessionSpec(
    agent=REF,
    agent_revision=Revision(local=2, native="n2"),
    config_revision=3,
    metadata={"daimon_chat_session_id": "c", "é": "ü"},
    resources=(
        ResourceBinding(
            id="r",
            kind="artifact",
            target_path="/workspace/a",
            resource=REF.model_copy(update={"kind": "artifact", "id": "file-1"}),
        ),
    ),
)


def test_config_revision_digests_are_unchanged() -> None:
    backends = (
        ResolvedBackend(backend="anthropic", profile="anthropic.managed_agents", model=None),
        ResolvedBackend(
            backend="openai",
            profile="openai.persistent_workspace",
            model="gpt-6-luna",
            thread_mode="shared",
        ),
    )
    assert [backend.content_digest() for backend in backends] == [
        "663d9f10dd4cb4f1a954eeedbaf69cf02fe55b2adf4707511d7176d0117fd221",
        "c07fc0ee2e9f6066c973db663b31509e36fc0e4d79170ea1cc54cb5ade2ded84",
    ]


def test_session_plan_digests_are_unchanged() -> None:
    controls = (
        SessionControls(),
        SessionControls(
            model="gpt-6-luna",
            multi_agent_enabled=False,
            container_size="small",
            spend_limit_usd_cents=500,
        ),
    )
    assert [
        request_digest({"spec": digest_body(SPEC), "controls": digest_body(each)})
        for each in controls
    ] == [
        "cf6481f5d84a6d4a6f4ee71ba12761970b158f8a278bc576c209bdb527540886",
        "6e4c35a94534665887c7a4a443e037e41b0dfdd6bc71565b3b32cd6bd0eab299",
    ]
    assert (
        request_digest({"spec": digest_body(SPEC), "model": "qwen"})
        == "645eaade7eb7453f4a6f48e252030a5527fe998719ad4e8ff2d55103b38afaeb"
    )


class _Inner(Contract):
    name: str


class _Before(Contract):
    items: tuple[_Inner, ...]


class _After(Contract):
    items: tuple[_Inner, ...]
    tier: str | None = additive(None)


class _Nested(Contract):
    inner: _After


def test_an_additive_field_counts_only_once_set() -> None:
    items = (_Inner(name="a"),)
    before = digest_body(_Before(items=items))
    assert digest_body(_After(items=items)) == before
    assert digest_body(_After(items=items, tier="x")) == {**before, "tier": "x"}
    assert digest_body(_Nested(inner=_After(items=items))) == {"inner": before}
