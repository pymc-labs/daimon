"""The `help` command: the text commands and how to reach the bot in chats and channels."""

from __future__ import annotations

from collections.abc import Collection

from daimon.adapters.teams.card_actions import heading, text_lines
from daimon.adapters.teams.commands import CommandContext
from microsoft_teams.cards import AdaptiveCard, CardElement, Fact, FactSet, TextBlock

# One line per command, in display order. Only registered commands are listed,
# so a command's line can land before the command does.
COMMAND_HELP = {
    "new": "Start a fresh conversation",
    "setup": "See your agents, who answers where, and make changes",
    "here": "Who answers where you send it, what it can read and which credentials it has",
    "routines": "Show and manage this organisation's scheduled routines",
    "memory": "List what the agent remembers; add a path to read one memory",
    "privacy": "See, export or delete what {bot} stores about you",
    "billing": "Your usage this month (admins see a per-member breakdown and top-ups)",
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
    talk = [
        "In our 1:1 chat, just type: every message goes to your agent.",
        f"In a channel, @mention {bot} in a post, and again in replies to continue it.",
        f"For example: @{bot} help me set up, or @{bot} make a routine that runs daily.",
    ]
    body: list[CardElement] = [
        heading("Commands"),
        TextBlock(text="Send these in our 1:1 chat.", is_subtle=True, wrap=True),
        FactSet(facts=facts),
        heading(f"💬 Or just talk to {bot}"),
        *text_lines(*talk),
    ]
    return AdaptiveCard(body=body, fallback_text=f"{bot} command reference")


async def send_help(context: CommandContext, *, names: Collection[str]) -> None:
    await context.send_card(help_card(names, bot=context.inbound.bot_name or "daimon"))
