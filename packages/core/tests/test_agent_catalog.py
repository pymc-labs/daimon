"""What the agent editor offers: allowed models, and no deleted channel."""

from __future__ import annotations

from daimon.core.agent_catalog import channels_of, models_of
from daimon.core.agent_model_names import AgentModel
from daimon.core.tenant_summary import ChannelSummary
from mux.contracts.ids import ModelRef


def _channel(channel_id: str) -> ChannelSummary:
    return ChannelSummary(
        channel_id=channel_id,
        agent_name=None,
        environment_name=None,
        own_agents_only=False,
        budget=None,
    )


def test_channels_of_leaves_out_a_deleted_channel_and_labels_the_rest() -> None:
    offered = channels_of(
        [_channel("c1"), _channel("c2"), _channel("c3")],
        platform="discord",
        names={"c1": "general", "c2": "Deleted channel"},
        gone={"c2"},
        is_admin=True,
    )
    assert [(c.id, c.name) for c in offered] == [("c1", "general"), ("c3", "Unnamed channel")], (
        "a gone channel is not offered; one never named stays unnamed"
    )


def test_models_of_lists_allowed_providers_and_marks_fast_ones() -> None:
    entries = [
        AgentModel(ModelRef(provider="anthropic", id="claude-opus-5-5"), "", 4.0, 20.0),
        AgentModel(ModelRef(provider="openai", id="gpt-6-astra"), "", 10.0, 50.0),
        AgentModel(ModelRef(provider="gemini", id="gemini-4-argon"), "", 2.0, 10.0),
    ]
    offered = models_of(
        entries,
        providers=("anthropic", "openai"),
        priced=("claude-opus-5-5", "claude-opus-5-5-fast"),
    )
    assert [(m.backend, m.id, m.label, m.fast) for m in offered] == [
        ("anthropic", "claude-opus-5-5", "Opus 5.5", True),
        ("openai", "gpt-6-astra", "GPT-6 Astra", False),
    ], "only allowed providers, each model once, named as people say it"
    assert (offered[1].in_usd, offered[1].out_usd) == (10.0, 50.0), "prices are numbers"
