"""The reply card names the model a turn runs on, not the agent's own."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from daimon.adapters.discord.bot import turn_model, turn_model_label
from daimon.adapters.discord.lifecycle import DiscordTurnLifecycle
from mux.contracts.config import BackendConfig, ConfigRevision, resolve_default
from mux.contracts.ids import ChannelRef

GEMINI = ConfigRevision.create(
    ChannelRef(tenant_id="tenant", platform="discord", channel_id="channel"),
    1,
    resolve_default(
        BackendConfig(backend="gemini", profile="gemini.inline_reuse", model="gemini-4-neon")
    ),
)


def _admission(backend: ConfigRevision | None) -> MagicMock:
    """An agent copy: its own (Managed Agents) model is still the Claude one it came from."""
    admission = MagicMock()
    admission.backend_revision = backend
    admission.agent.model.id = "claude-opus-5-5"
    admission.agent.model.model_extra = {"effort": "high"}
    return admission


def _card_label(admission: MagicMock) -> str:
    lifecycle = DiscordTurnLifecycle(
        send=AsyncMock(),
        edit=AsyncMock(),
        agent_name="gemini-copy",
        # As the mention, continuation and recovery paths build the card.
        model_id=turn_model_label(admission),
    )
    return lifecycle._state.model_label  # pyright: ignore[reportPrivateUsage]


def test_a_gemini_copy_turn_is_labelled_with_the_gemini_model() -> None:
    admission = _admission(GEMINI)
    assert turn_model(admission) == ("gemini-4-neon", GEMINI.effort), "as the reactions show"
    assert _card_label(admission) == "gemini-4-neon", "not the Claude model it was copied from"


def test_a_default_backend_turn_keeps_the_agents_model() -> None:
    admission = _admission(None)
    assert turn_model(admission) == ("claude-opus-5-5", "high")
    assert _card_label(admission) == "Opus 5.5"


# A card built with a fixed label, and why.
FIXED_LABELS = {"lifecycle.py": "'recovery'"}  # the dead-session recovery card names no model


def test_every_reply_card_takes_its_label_from_the_turns_model() -> None:
    """One rule on every path: each `DiscordTurnLifecycle(...)` labels from `turn_model_label`."""
    import ast
    from pathlib import Path

    import daimon.adapters.discord as package

    root = Path(package.__file__).parent
    seen: list[str] = []
    for path in sorted(root.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name | ast.Attribute)
                and (node.func.id if isinstance(node.func, ast.Name) else node.func.attr)
                == "DiscordTurnLifecycle"
            ):
                continue
            label = next((k.value for k in node.keywords if k.arg == "model_id"), None)
            where = f"{path.relative_to(root)}:{node.lineno}"
            assert label is not None, f"{where} builds a card with no model label"
            source = ast.unparse(label)
            seen.append(where)
            if FIXED_LABELS.get(path.name) == source:
                continue
            assert any(
                isinstance(n, ast.Call)
                and isinstance(n.func, ast.Name)
                and n.func.id == "turn_model_label"
                for n in ast.walk(label)
            ), f"{where} labels the card with {source}, not the turn's model"
    assert len(seen) >= 8, "the mention, continuation, recovery, wizard and DM paths are all found"
