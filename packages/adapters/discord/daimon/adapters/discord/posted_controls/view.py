"""Discord renderer for a `PostedCard`, on the editing side.

The Discord bot process (worker VM) re-renders a posted card in place once
the form comes back; the MCP process (Cloud Run) posted it in the first
place. The two cannot import each other (import-linter's independence
contract), and `daimon.core.posted_controls` deliberately carries no Discord
SDK type, so each side owns a copy of this translation —
`daimon.adapters.mcp.tools.discord._posted_card` is the other one. The two
bodies are byte-identical on purpose and
`tests/parity/test_posted_card_renderer_parity.py` is the executable guard:
a divergent copy here would silently desync what a user sees when the card
is posted from what they see the moment it is edited, with nothing raising.

A `Container`, `TextDisplay` or `Separator` cannot live in a plain
`discord.ui.View` — those are components-v2 items — so `LayoutView` is the
only view type built here. That is also why the card carries no message
`content`: a components-v2 message may not have any, and a components-v2
message can never be edited back to a classic view.
"""

from __future__ import annotations

from daimon.adapters.discord.theme import COLOR_AMBER
from daimon.core.posted_controls import CardButton, PostedCard

import discord

__all__ = ["build_card_view"]


def _build_card_button(button: CardButton) -> discord.ui.Button[discord.ui.LayoutView]:
    """One `CardButton` as a real button.

    A link button carries a url and nothing else; a form button carries the
    request token's `custom_id`, which is what the bot process matches its
    `DynamicItem` template against on a click.
    """
    if button.url is not None:
        return discord.ui.Button(label=button.label, url=button.url)
    return discord.ui.Button(
        style=discord.ButtonStyle.primary, label=button.label, custom_id=button.custom_id
    )


def _card_footer(card: PostedCard) -> str | None:
    """The card's footer as Discord renders it, or `None` when it has none.

    Only the `requested` footer is a template: it names the requester as a
    mention and the expiry as a relative timestamp, both of which are Discord
    spellings core cannot produce. Every other state's footer is final text.
    """
    if card.footer is None:
        return None
    if card.state != "requested":
        return card.footer
    if card.requester_platform_user_id is None or card.expires_at_unix is None:
        raise ValueError("a requested card must carry a requester and an expiry")
    return card.footer.format(
        requester=f"<@{card.requester_platform_user_id}>",
        expires=f"<t:{card.expires_at_unix}:R>",
    )


def build_card_view(card: PostedCard) -> discord.ui.LayoutView:
    """Render one `PostedCard` as the V2 `LayoutView` Discord posts and edits.

    Component order is a contract: the bolded headline, the facts as one
    subtext block, then — only when the card has buttons — an invisible
    separator and one action row, then the footer as subtext. Every state
    wears the same amber accent: the headline emoji is the state marker, so
    nothing here ever turns green on success.
    """
    container: discord.ui.Container[discord.ui.LayoutView] = discord.ui.Container(
        accent_colour=discord.Colour(COLOR_AMBER)
    )
    container.add_item(discord.ui.TextDisplay(f"**{card.headline}**"))
    if card.facts:
        container.add_item(discord.ui.TextDisplay("\n".join(f"-# {fact}" for fact in card.facts)))
    if card.buttons:
        container.add_item(discord.ui.Separator(visible=False))
        action_row: discord.ui.ActionRow[discord.ui.LayoutView] = discord.ui.ActionRow()
        for button in card.buttons:
            action_row.add_item(_build_card_button(button))
        container.add_item(action_row)
    footer = _card_footer(card)
    if footer is not None:
        container.add_item(discord.ui.TextDisplay(f"-# {footer}"))
    view = discord.ui.LayoutView(timeout=None)
    view.add_item(container)
    return view
