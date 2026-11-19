"""What a switch turns off, said once as information after it applies."""

from __future__ import annotations

import uuid

from daimon.core.model_switch import ModelSwitchPreview
from daimon.core.stores.domain import CompatibilityPreview
from daimon.core.switch_card import loss_words, switch_lines
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
        declared_drops=drops,
        skill_names={"sk_1": "Pricing"},
    )


def _switch(preview: CompatibilityPreview | None) -> ModelSwitchPreview:
    return ModelSwitchPreview(
        model=ModelRef(provider="openai", id="gpt-5"),
        label="GPT-5",
        before="anthropic",
        preview=preview,
    )


def test_a_switch_that_loses_nothing_says_nothing() -> None:
    """No drop and no shared-thread limit: no line at all, like a pick between Claude models."""
    assert _switch(None).lines() == (), "Claude to Claude has no preview"
    assert _switch(_preview()).lines() == (), "a lossless cross-provider pick"
    assert _switch(None).allowed, "a pick without a preview is allowed"


def test_what_is_off_is_one_line_of_information_then_any_limit() -> None:
    """The losses name the provider and say they come back; no question, no price."""
    lines = switch_lines(_preview(("memory", "skill:sk_1"), limits=(_LIMIT,)))
    assert lines == (
        "Off on OpenAI: memory and Pricing skill. They come back when you switch back.",
        _LIMIT,
    ), lines
    assert not any("?" in line for line in lines), "information, never a question"
    assert "Costs" not in " ".join(lines), "the price stays on the model select"
    assert switch_lines(_preview(limits=(_LIMIT,))) == (_LIMIT,), "no empty loss line"


def test_lines_escape_names_for_the_surface() -> None:
    """A surface's escape reaches every name it shows."""
    lines = _switch(_preview(("memory",), limits=(_LIMIT,))).lines(escape=str.upper)
    assert lines[0].endswith("MEMORY. They come back when you switch back."), lines
    assert lines[1] == _LIMIT.upper(), lines


def test_a_pick_that_is_off_here_is_not_allowed() -> None:
    """The provider gate stays: a refused target reads as not allowed."""
    assert not _switch(_preview(allowed=False)).allowed, "a provider off here never applies"


def test_the_loss_words_name_saved_keys_first() -> None:
    """Plain words, joined with 'and', the costliest loss first, skills by name."""
    words = loss_words(("memory", "skill:sk_1", "credentials"), {"sk_1": "Pricing"})
    assert words == "saved keys, memory and Pricing skill", words
    assert loss_words(("memory",)) == "memory", "one loss is a single word"
    assert loss_words(tuple(f"mcp_server:{'m' * 60}{i}" for i in range(20))).endswith("…"), (
        "a long list is clipped"
    )
