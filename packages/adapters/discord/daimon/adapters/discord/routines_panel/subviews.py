"""Last-output sub-view and container builder for /routines."""

from __future__ import annotations

from daimon.adapters.discord import layout
from daimon.adapters.discord.routines_panel.state import derive_state, state_label
from daimon.core.stores.domain import RoutineRow

import discord

_MAX_OUTPUT_CHARS = 1000


def build_last_output_container(
    routine: RoutineRow,
) -> discord.ui.Container[discord.ui.LayoutView]:
    """V2 container for the last-output sub-view.

    Structure:
    - ``## 📜 Last output`` header with ``-# {glyph} {state} · {label}`` subtext
    - hairline separator
    - fenced code block TextDisplay (truncated to ``_MAX_OUTPUT_CHARS``)
    """
    glyph, _color = derive_state(routine)
    label = routine.trigger_message.strip()[:60] or routine.id.hex[:8]
    subtext = f"{glyph} {state_label(glyph)} · {label}"

    if routine.last_error is not None:
        body = routine.last_error
    else:
        body = routine.last_result_tail or "(no output)"

    if len(body) > _MAX_OUTPUT_CHARS:
        body = body[:_MAX_OUTPUT_CHARS] + "\n… (truncated)"

    return discord.ui.Container(
        layout.header("📜 Last output", subtext=subtext),
        layout.hairline(),
        discord.ui.TextDisplay(f"```\n{body}\n```"),
    )


class _BackButton(discord.ui.Button["ViewLastOutputSubView"]):
    def __init__(self) -> None:
        super().__init__(label="← Back", style=discord.ButtonStyle.secondary)

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        await interaction.delete_original_response()


class ViewLastOutputSubView(discord.ui.LayoutView):
    """LayoutView wrapping the last-output container + a ← Back ActionRow."""

    def __init__(self, routine: RoutineRow, *, allowed_user_id: int) -> None:
        super().__init__(timeout=300)
        self.routine = routine
        self.allowed_user_id = allowed_user_id

        container = build_last_output_container(routine)
        back_row: discord.ui.ActionRow[ViewLastOutputSubView] = discord.ui.ActionRow(_BackButton())
        container.add_item(layout.hairline())
        container.add_item(back_row)
        self.add_item(container)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:  # type: ignore[override]  # base uses broader Interaction[Client] type
        if interaction.user.id != self.allowed_user_id:
            await interaction.response.send_message(
                "Only the command invoker can use these buttons.",
                ephemeral=True,
            )
            return False
        return True
