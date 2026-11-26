"""The running card names the model the reply runs on, on every chat path.

A conversation on another provider runs the model of its backend revision, never
the agent's own record, so every place that builds a turn card must take its model
from `shown_model` (`daimon.core.turn.shown_model`). Callers are found from the
source: any call of a `*TurnLifecycle` class in an adapter.
"""

from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

import pytest
from daimon.core.turn.shown_model import card_label, shown_model

ROOT = Path(__file__).resolve().parents[2] / "packages" / "adapters"

# A lifecycle built to finish an already-posted card has no admission and no model to show.
NO_MODEL = {("discord/daimon/adapters/discord/lifecycle.py", "recovery")}


def _is_shown_model_id(node: ast.expr, bound: set[str]) -> bool:
    if isinstance(node, ast.Name):
        return node.id in bound
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "id"
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Name)
        and node.value.func.id == "shown_model"
    )


def _lifecycle_calls() -> list[tuple[str, ast.Call, set[str]]]:
    found: list[tuple[str, ast.Call, set[str]]] = []
    for path in sorted(ROOT.glob("*/daimon/adapters/*/**/*.py")):
        tree = ast.parse(path.read_text())
        bound = {
            target.id
            for node in ast.walk(tree)
            if isinstance(node, ast.AnnAssign | ast.Assign)
            and node.value is not None
            and _is_shown_model_id(node.value, set())
            for target in ([node.target] if isinstance(node, ast.AnnAssign) else node.targets)
            if isinstance(target, ast.Name)
        }
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Name) and func.id in {
                "DiscordTurnLifecycle",
                "SlackTurnLifecycle",
                "TeamsTurnLifecycle",
            }:
                found.append((str(path.relative_to(ROOT)), node, bound))
    return found


def test_every_turn_card_takes_its_model_from_shown_model() -> None:
    calls = _lifecycle_calls()
    assert len(calls) >= 10, "the scan finds the Discord, Slack and Teams card builders"
    wrong: list[str] = []
    for where, call, bound in calls:
        model = next((kw.value for kw in call.keywords if kw.arg == "model_id"), None)
        if model is None:
            wrong.append(f"{where}:{call.lineno} names no model_id")
        elif isinstance(model, ast.Constant):
            if (where, model.value) not in NO_MODEL:
                wrong.append(f"{where}:{call.lineno} shows the fixed model {model.value!r}")
        elif not _is_shown_model_id(model, bound):
            wrong.append(f"{where}:{call.lineno} does not take its model from shown_model")
    assert not wrong, "\n".join(wrong)


def _admission(*, profile: str | None, model: str | None) -> SimpleNamespace:
    revision = None if profile is None else SimpleNamespace(profile=profile, model=model)
    return SimpleNamespace(
        backend_revision=revision,
        agent=SimpleNamespace(model=SimpleNamespace(id="claude-opus-5-5")),
    )


@pytest.mark.parametrize(
    ("profile", "model", "shown_id", "label", "provider_label"),
    [
        (None, None, "claude-opus-5-5", "Opus 5.5", "Anthropic"),
        ("openai.persistent_workspace", "gpt-6-luna", "gpt-6-luna", "GPT-6 Luna", "OpenAI"),
        ("gemini.inline_reuse", "gemini-4-argon", "gemini-4-argon", "Gemini 4 Argon", "Gemini"),
        ("gemini.inline_reuse", None, "gemini-3.8-flash", "Gemini 3.8 Flash", "Gemini"),
    ],
)
def test_a_reply_off_anthropic_shows_its_own_model_and_provider(
    profile: str | None, model: str | None, shown_id: str, label: str, provider_label: str
) -> None:
    shown = shown_model(_admission(profile=profile, model=model))  # pyright: ignore[reportArgumentType]

    assert (shown.id, shown.label, shown.provider_label) == (shown_id, label, provider_label)
    assert card_label(shown.id) == label, "the card names the id the same way"
