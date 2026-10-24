"""The `help` command: the words to send, where they work, and how to reach the bot.

The card depends on where `help` was sent. A 1:1 chat lists every registered
command. A channel lists only the commands answered there (`IN_THREAD`) and names
the 1:1 chat for the rest; a group chat also lists `new`, which resets it.
"""

from __future__ import annotations

from collections.abc import Collection

from daimon.adapters.teams.card_actions import heading
from daimon.adapters.teams.commands import CommandContext
from daimon.adapters.teams.private_replies import IN_THREAD
from daimon.core.platform_words import DESCRIPTIONS, WORDS, Action, Place
from microsoft_teams.cards import AdaptiveCard, CardElement, Fact, FactSet, TextBlock

_TEAMS = WORDS["teams"]

# Display order. Only registered commands are listed, so a command's line can
# land before the command does.
_ORDER: tuple[Action, ...] = (
    "new",
    "setup",
    "memory",
    "routines",
    "billing",
    "privacy",
    "here",
    "support",
    "help",
)
COMMAND_HELP = {_TEAMS.commands[action].word: DESCRIPTIONS[action] for action in _ORDER}

_CHAT_ONLY = [
    _TEAMS.commands[action].word for action in _ORDER if _TEAMS.commands[action].reach == "chat"
]


def _answered_in(place: Place) -> frozenset[str]:
    """The command words answered where `help` was sent."""
    if place == "dm":
        return frozenset(COMMAND_HELP)
    return IN_THREAD | {"new"} if place == "group" else IN_THREAD


def _note(text: str) -> TextBlock:
    return TextBlock(text=text, spacing="Medium", wrap=True)


def help_card(
    names: Collection[str], *, bot: str, place: Place = "dm", foreign: bool = False
) -> AdaptiveCard:
    """The commands registered and answered in `place`, then how to talk to the agent.

    A `foreign` asker (from another organisation) has no 1:1 chat with Daimon, so the
    commands that need one are for someone in the organisation that added it."""
    answered = _answered_in(place)
    facts = [
        Fact(title=name, value=line)
        for name, line in COMMAND_HELP.items()
        if name in names and name in answered
    ]
    elsewhere: list[CardElement] = []
    if place == "dm":
        intro = "Send one of these words."
        ask = "Help me set up"
    else:
        intro = f"Only you can see this. @mention {bot} with one of these words."
        ask = f"@mention {bot} with your question."
        in_chat = [word for word in _CHAT_ONLY if word in names]
        if in_chat and foreign:
            elsewhere.append(_note(f"For {', '.join(in_chat)}, ask {_TEAMS.host_admin}."))
        elif in_chat:
            elsewhere.append(_note(f"In our {_TEAMS.private_chat}: {', '.join(in_chat)}"))
        if place == "channel":
            elsewhere.append(_note(f"To start over, {_TEAMS.fresh_start(place=place)}."))
    body: list[CardElement] = [
        heading("📖 Commands"),
        TextBlock(text=intro, is_subtle=True, wrap=True),
        FactSet(facts=facts),
        *elsewhere,
        heading("💬 Or just ask"),
        _note(ask),
    ]
    return AdaptiveCard(body=body, fallback_text=f"{bot} command reference")


async def send_help(context: CommandContext, *, names: Collection[str]) -> None:
    """Answer in the card for where `help` was sent: `asked_in` when it moved to the 1:1 chat."""
    place = (context.asked_in or context.inbound).kind
    await context.send_card(
        help_card(
            names,
            bot=context.inbound.bot_name or "daimon",
            place=place,
            foreign=context.inbound.is_foreign,
        )
    )
