"""The product skill cap fits what every backend can carry."""

from __future__ import annotations

from typing import cast

import pytest
from daimon.core.constants import AGENT_SKILL_CAP
from mux.contracts.ids import ModelRef, SkillRef
from mux.contracts.resources import AgentSpec
from mux.drivers.openai._common import Context
from mux.drivers.openai.agents import agent_body
from mux.drivers.openai.skill_bindings import decode, encode_metadata
from mux.drivers.openai.transport import Transport
from mux.errors import UnsupportedCapability

# The longest skill id a pin carries, and a few keys of the agent's own metadata.
_ID_LENGTH = 54
_OWN_METADATA = {f"daimon_key_{i}": "x" * 64 for i in range(3)}
_NATIVE_METADATA_KEYS = 16


def _pins(count: int, *, digest: bool) -> tuple[SkillRef, ...]:
    return tuple(
        SkillRef(
            id=f"skill_{'x' * (_ID_LENGTH - 8)}{n:02}",
            version="12",
            digest="a" * 64 if digest else None,
        )
        for n in range(count)
    )


def _context() -> Context:
    return Context(cast(Transport, None), "offline", "openai.persistent_workspace", None)


def test_openai_agent_metadata_carries_the_cap_of_digest_pinned_skills() -> None:
    """An operator-pinned agent keeps each skill's content digest, so pins are the longest."""
    pins = _pins(AGENT_SKILL_CAP, digest=True)
    spec = AgentSpec(
        name="agent",
        model=ModelRef(provider="openai", id="gpt-5"),
        skills=pins,
        metadata=_OWN_METADATA,
    )
    metadata = agent_body(spec, _context())["metadata"]
    assert isinstance(metadata, dict)
    assert len(metadata) < _NATIVE_METADATA_KEYS, "one native key stays free for the tenant marker"
    assert decode(metadata) == pins, "every pin and digest reads back"


def test_openai_session_metadata_carries_the_cap_of_skills() -> None:
    """A session's pins carry no digest; its own two keys and the agent's metadata come first."""
    pins = _pins(AGENT_SKILL_CAP, digest=False)
    metadata = {**_OWN_METADATA, "mux_tenant": "t", "mux_config_revision": "1"}
    metadata.update(encode_metadata(pins, _context()))
    assert len(metadata) <= _NATIVE_METADATA_KEYS, "the session metadata fits the native keys"
    assert decode(metadata) == pins


def test_a_pin_list_far_past_the_cap_is_refused_rather_than_cut() -> None:
    with pytest.raises(UnsupportedCapability):
        encode_metadata(_pins(AGENT_SKILL_CAP * 4, digest=True), _context())
