"""The `help` command: the text commands and how to reach the bot in chats and channels."""

from __future__ import annotations

from collections.abc import Collection

from daimon.adapters.teams.card_actions import heading
from daimon.adapters.teams.commands import CommandContext
from microsoft_teams.cards import AdaptiveCard, CardElement, Fact, FactSet, TextBlock

# One line per command, in display order. Only registered commands are listed,
# so a command's line can land before the command does.
COMMAND_HELP = {
    "new": "Start a fresh conversation",
    "setup": "Your agents and where they answer",
    "memory": "What the agent remembers",
    "routines": "Scheduled jobs",
    "billing": "Your usage and credit",
    "privacy": "See, export or delete your data",
    "here": "Who answers here and what they can access",
    "support": "Ask a person",
    "help": "This list",
}


def help_card(names: Collection[str], *, bot: str) -> AdaptiveCard:
    """The registered commands, then how to talk to the agent."""
    facts = [
        Fact(title=name, value=line.format(bot=bot))
        for name, line in COMMAND_HELP.items()
        if name in names
    ]
    body: list[CardElement] = [
        heading("📖 Commands"),
        TextBlock(text="Send these in our 1:1 chat.", is_subtle=True, wrap=True),
        FactSet(facts=facts),
        heading("💬 Or just ask"),
        TextBlock(text=f"In a channel, @mention {bot} each time.", spacing="Medium", wrap=True),
        TextBlock(text="In our 1:1 chat, just type.", spacing="Medium", wrap=True),
    ]
    return AdaptiveCard(body=body, fallback_text=f"{bot} command reference")


async def send_help(context: CommandContext, *, names: Collection[str]) -> None:
    await context.send_card(help_card(names, bot=context.inbound.bot_name or "daimon"))
