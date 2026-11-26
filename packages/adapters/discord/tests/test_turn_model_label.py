"""The reply card names the model a turn runs on, not the agent's own."""

from __future__ import annotations

from unittest.mock import MagicMock

from daimon.adapters.discord.bot import turn_model
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
    async def send(**_: object) -> object:
        return object()

    async def edit(_: object, **__: object) -> None:
        return None

    lifecycle = DiscordTurnLifecycle(
        send=send,
        edit=edit,
        agent_name="gemini-copy",
        # As the mention, continuation and recovery paths build the card.
        model_id=turn_model(admission)[0] or "",
    )
    return lifecycle._state.model_label  # pyright: ignore[reportPrivateUsage]


def test_a_gemini_copy_turn_is_labelled_with_the_gemini_model() -> None:
    admission = _admission(GEMINI)
    assert turn_model(admission) == ("gemini-4-neon", GEMINI.effort)
    assert _card_label(admission) == "gemini-4-neon", "not the Claude model it was copied from"


def test_a_default_backend_turn_keeps_the_agents_model() -> None:
    admission = _admission(None)
    assert turn_model(admission) == ("claude-opus-5-5", "high")
    assert _card_label(admission) == "Opus 5.5"
