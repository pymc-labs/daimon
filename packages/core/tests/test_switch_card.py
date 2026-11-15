"""The one switch-card rule and the short card every surface shows."""

from __future__ import annotations

import uuid

from daimon.core.model_switch import ModelSwitchPreview, switch_card_text
from daimon.core.stores.domain import CompatibilityPreview
from daimon.core.switch_card import loss_words, needs_switch_card, switch_card_lines
from mux.contracts.ids import ModelRef

_LIMIT = "Shared threads are unavailable on this selection."


def _preview(
    drops: tuple[str, ...] = (), *, limits: tuple[str, ...] = (), allowed: bool = True
) -> CompatibilityPreview:
    return CompatibilityPreview(
        tenant_id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        target_backend="openai",
        setting_revision=3,
        policy_revision="policy",
        unsupported_skills=(),
        unsupported_mcps=(),
        unsupported_capabilities=(),
        shared_thread_limits=limits,
        cost_line="Costs about the same per message",
        allowed=allowed,
        digest="d" * 64,
        drop_consent={
            "declared_drops": list(drops),
            "source_pin": "a" * 64,
            "target_digest": "b" * 64,
        },
        skill_names={"sk_1": "Pricing"},
    )


def _switch(preview: CompatibilityPreview | None) -> ModelSwitchPreview:
    return ModelSwitchPreview(
        model=ModelRef(provider="openai", id="gpt-5"),
        label="GPT-5",
        before="anthropic",
        preview=preview,
    )


def test_a_switch_that_loses_nothing_needs_no_card() -> None:
    """No drop and no shared-thread limit: the pick applies at once, like a Claude model."""
    assert not needs_switch_card(None), "Claude to Claude has no preview"
    assert not needs_switch_card(_preview()), "a lossless cross-provider pick"
    assert not _switch(_preview()).asks_first, "the pick's own rule agrees"


def test_any_drop_or_shared_limit_needs_the_card() -> None:
    """Every declared drop counts, web search included; so does a shared-thread limit."""
    assert needs_switch_card(_preview(("builtin_tool:web_search",))), "web search alone asks"
    assert needs_switch_card(_preview(("memory",))), "memory asks"
    assert needs_switch_card(_preview(limits=(_LIMIT,))), "a shared-thread limit asks"
    assert _switch(_preview(allowed=False)).asks_first, "a pick that is off here never applies"


def test_the_loss_words_name_saved_keys_first() -> None:
    """Plain words, joined with 'and', the costliest loss first, skills by name."""
    words = loss_words(("memory", "skill:sk_1", "credentials"), {"sk_1": "Pricing"})
    assert words == "saved keys, memory and Pricing skill", words
    assert loss_words(("memory",)) == "memory", "one loss is a single word"
    assert loss_words(tuple(f"mcp_server:{'m' * 60}{i}" for i in range(20))).endswith("…"), (
        "a long list is clipped"
    )


def test_the_card_is_a_title_and_one_line_per_fact() -> None:
    """Title, what stops working, any limit; no price, no None lines, no category labels."""
    text = switch_card_text(_switch(_preview(("memory",), limits=(_LIMIT,))), agent_name="Ada")
    assert text == ("Switch Ada to GPT-5?", "Stops working: memory", _LIMIT), text
    assert "Costs" not in " ".join(text), "the price stays on the model select"
    assert switch_card_lines(_preview(limits=(_LIMIT,))) == (_LIMIT,), "no empty loss line"
    assert switch_card_text(_switch(_preview()), agent_name=None) == (
        "Switch this conversation to GPT-5?",
    ), "a conversation pick names the conversation"
