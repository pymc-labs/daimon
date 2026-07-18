"""Credentials sub-view (LayoutView) for /agent-setup.

Paste-only KEY=VALUE secrets management for the selected agent. V2 migration
V2 migration: classic View → LayoutView, build_credentials_embed replaced with
build_credentials_container.

Secret hygiene is enforced structurally, not by convention:
- values never reach the container (the builder takes key NAMES only),
- values never reach the logs (log calls record key names / counts only),
- values never reach the Discord CDN (the modal is a TextInput; there is no
  URL-fetch path and no attachment path),
- values never reach a custom_id (the remove-select option carries the key name only).

`tenant_id` and `agent_id` are resolved once in `EditView._on_secrets` and
threaded down here as explicit constructor args — the sub-view never re-derives
the tenant from user input.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Awaitable, Callable

import structlog
from daimon.adapters.discord.agent_setup.state import PanelState
from daimon.adapters.discord.errors import generate_request_id, render_error
from daimon.adapters.discord.layout import hairline, header
from daimon.adapters.discord.runtime import DiscordRuntime
from daimon.core.stores.agent_files import (
    delete_agent_file,
    list_agent_files,
    put_agent_file,
)

import discord

_log = structlog.get_logger()

_SECRET_CAP = 20
_MAX_SECRET_VALUE_BYTES = 4096
_POSIX_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# Re-render callback: invoked after a successful paste so the sub-view can
# reload and re-render in place. Takes the modal's interaction.
OnAdded = Callable[[discord.Interaction], Awaitable[None]]


def build_credentials_container(
    *,
    agent_name: str,
    secret_names: list[str],
    is_system: bool,
) -> discord.ui.Container[discord.ui.LayoutView]:
    """Pure: render the Credentials container from key NAMES only.

    Never receives or renders a secret value — every value is omitted entirely
    Key names render as `KEY` chips on a single line separated by · .
    """
    container: discord.ui.Container[discord.ui.LayoutView] = discord.ui.Container()
    container.add_item(
        header(
            f"🔑 Secrets — {agent_name}",
            subtext="values are write-only; only key names are shown",
        )
    )
    container.add_item(hairline())

    if secret_names:
        chips = " · ".join(f"`{name}`" for name in secret_names)
        container.add_item(discord.ui.TextDisplay(chips))
    else:
        # Design-language collapse: dim hint instead of "(none)" copy.
        container.add_item(discord.ui.TextDisplay("-# ＋ add your first secret"))

    return container


class PasteSecretModal(discord.ui.Modal, title="Add secrets"):
    """Paste KEY=VALUE lines → per-key ``put_agent_file``.

    No URL fetch, no attachment, no HTTP: pasted bytes go modal text → store
    only.
    """

    def __init__(
        self,
        *,
        runtime: DiscordRuntime,
        tenant_id: uuid.UUID,
        agent_id: uuid.UUID,
        on_added: OnAdded,
    ) -> None:
        super().__init__()
        # Modal has no `.view` reference — store deps explicitly.
        self._runtime = runtime
        self._tenant_id = tenant_id
        self._agent_id = agent_id
        self._on_added = on_added
        self.content_input: discord.ui.TextInput[PasteSecretModal] = discord.ui.TextInput(
            label="KEY=VALUE lines",
            style=discord.TextStyle.paragraph,
            max_length=4000,
            required=True,
            placeholder="XERO_API_KEY=abc123\nTOGGL_TOKEN=xyz789",
        )
        self.add_item(self.content_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        raw = str(self.content_input.value or "")

        pairs: list[tuple[str, str]] = []
        for line in raw.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if "=" not in stripped:
                continue
            key, _, value = stripped.partition("=")
            key = key.strip()
            if not _POSIX_KEY_RE.match(key):
                await interaction.followup.send(
                    "Secret name must match `[A-Za-z_][A-Za-z0-9_]*` "
                    "(letters, digits, underscores; must not start with a digit). "
                    "Fix and re-paste.",
                    ephemeral=True,
                )
                return
            if len(value.encode()) > _MAX_SECRET_VALUE_BYTES:
                await interaction.followup.send(
                    f"Secret value for `{key}` is too large. Max {_MAX_SECRET_VALUE_BYTES} bytes.",
                    ephemeral=True,
                )
                return
            pairs.append((key, value))

        if not pairs:
            await interaction.followup.send("No valid KEY=VALUE lines found.", ephemeral=True)
            return

        # Log key NAMES only — never values.
        _log.info(
            "credentials.paste.submit",
            key_names=[k for k, _ in pairs],
            key_count=len(pairs),
        )
        try:
            async with self._runtime.sessionmaker() as session, session.begin():
                for key, value in pairs:
                    await put_agent_file(
                        session,
                        tenant_id=self._tenant_id,
                        agent_id=self._agent_id,
                        key=key,
                        content=value,
                    )
        except Exception:
            _log.exception("credentials.paste.failed", key_count=len(pairs))
            await interaction.followup.send(
                "Something went wrong — please try again.", ephemeral=True
            )
            return

        n = len(pairs)
        if n == 1:
            toast = f"Added `{pairs[0][0]}`. Takes effect on the next session."
        else:
            toast = f"Added {n} secrets. Takes effect on the next session."
        await interaction.followup.send(toast, ephemeral=True)
        await self._on_added(interaction)


class CredentialsSubView(discord.ui.LayoutView):
    """F5 Components V2 secrets sub-view opened from EditView's Secrets button.

    Container: ## 🔑 Secrets — {agent} + write-only subtext, KEY chips on one
    line, ✕ Remove a secret… select, + Add secrets · ← Back button row.

    Carries key NAMES only (``secret_names``) — never values. Mutations
    re-render this view in place via ``edit_original_response``; '← Back'
    replaces the message with ``EditView`` (it does NOT delete it), preserving
    the ephemeral isolation invariant.
    """

    def __init__(
        self,
        *,
        runtime: DiscordRuntime,
        state: PanelState,
        allowed_user_id: int,
        tenant_id: uuid.UUID,
        agent_id: uuid.UUID,
        secret_names: list[str],
        is_system: bool,
    ) -> None:
        super().__init__(timeout=300)
        self._runtime = runtime
        self._state = state
        self._allowed_user_id = allowed_user_id
        self._tenant_id = tenant_id
        self._agent_id = agent_id
        self._secret_names = secret_names
        self._is_system = is_system
        self._build_items()

    def _agent_name(self) -> str:
        return self._state.selected.name if self._state.selected else "?"

    def _build_items(self) -> None:
        self.clear_items()

        container = build_credentials_container(
            agent_name=self._agent_name(),
            secret_names=self._secret_names,
            is_system=self._is_system,
        )

        # ✕ Remove a secret… select (cap 20, glyph in placeholder not emoji=).
        remove_select = _build_remove_select(self._secret_names, is_system=self._is_system)
        remove_select.callback = self._make_remove_cb(remove_select)  # type: ignore[method-assign]

        select_row: discord.ui.ActionRow[CredentialsSubView] = discord.ui.ActionRow()
        select_row.add_item(remove_select)
        container.add_item(select_row)

        # Button row: + Add secrets · ← Back.
        btn_row: discord.ui.ActionRow[CredentialsSubView] = discord.ui.ActionRow()

        add_btn: discord.ui.Button[CredentialsSubView] = discord.ui.Button(
            label="+ Add secrets",
            style=discord.ButtonStyle.success,
            disabled=self._is_system or len(self._secret_names) >= _SECRET_CAP,
        )
        add_btn.callback = self._on_add  # type: ignore[method-assign]
        btn_row.add_item(add_btn)

        back_btn: discord.ui.Button[CredentialsSubView] = discord.ui.Button(
            label="← Back",
            style=discord.ButtonStyle.secondary,
        )
        back_btn.callback = self._on_back  # type: ignore[method-assign]
        btn_row.add_item(back_btn)

        container.add_item(btn_row)
        self.add_item(container)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:  # type: ignore[override]  # base uses broader Interaction[Client] type
        if interaction.user.id != self._allowed_user_id:
            await interaction.response.send_message(
                "Only the command invoker can use these buttons.", ephemeral=True
            )
            return False
        return True

    def _make_remove_cb(
        self, select: discord.ui.Select[CredentialsSubView]
    ) -> Callable[[discord.Interaction], Awaitable[None]]:
        async def _cb(interaction: discord.Interaction) -> None:
            if select.values[0] == "__none__":
                return
            await self._on_remove(interaction, select.values[0])

        return _cb

    async def _on_remove(self, interaction: discord.Interaction, key_name: str) -> None:
        # key_name is the secret KEY NAME the option carried — never a value.
        _log.info("credentials.remove.click", key=key_name)
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            async with self._runtime.sessionmaker() as session, session.begin():
                await delete_agent_file(
                    session,
                    tenant_id=self._tenant_id,
                    agent_id=self._agent_id,
                    key=key_name,
                )
        except Exception as err:
            rid = generate_request_id()
            _log.exception("credentials.remove.failed", key=key_name, request_id=rid)
            await interaction.followup.send(render_error(err, request_id=rid), ephemeral=True)
            return
        await self._reload_and_rerender(interaction)
        await interaction.followup.send(f"Removed `{key_name}`.", ephemeral=True)

    async def _on_add(self, interaction: discord.Interaction) -> None:
        _log.info("credentials.add.click")
        await interaction.response.send_modal(
            PasteSecretModal(
                runtime=self._runtime,
                tenant_id=self._tenant_id,
                agent_id=self._agent_id,
                on_added=self._reload_and_rerender,
            )
        )

    async def _on_back(self, interaction: discord.Interaction) -> None:
        # edit_message replaces in-place; it does NOT delete the message.
        # Construct the EditView so its container carries the ## ✏️ Editing header
        # (behavior-neutral correction of the pre-V2 panel-content quirk).
        from daimon.adapters.discord.agent_setup.edit_view import EditView

        await interaction.response.edit_message(
            view=EditView(
                self._state,
                runtime=self._runtime,
                allowed_user_id=self._allowed_user_id,
            ),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def _reload_and_rerender(self, interaction: discord.Interaction) -> None:
        async with self._runtime.sessionmaker() as session:
            rows = await list_agent_files(
                session, tenant_id=self._tenant_id, agent_id=self._agent_id
            )
        self._secret_names = [row.key for row in rows]
        self._build_items()
        await interaction.edit_original_response(
            view=self,
            allowed_mentions=discord.AllowedMentions.none(),
        )


def _build_remove_select(
    secret_names: list[str], *, is_system: bool
) -> discord.ui.Select[CredentialsSubView]:
    """✕ Remove a secret… select listing every key (no cap).

    Each option's ``label``/``value`` carries ONLY the secret KEY NAME — never a
    value, never a per-key custom_id. Disabled (empty placeholder) when
    there are no secrets; disabled (mutation-blocked) for system agents.
    """
    if len(secret_names) == 0:
        return discord.ui.Select(
            placeholder="(no secrets — use + Add secrets)",
            min_values=1,
            max_values=1,
            options=[discord.SelectOption(label="(no secrets)", value="__none__")],
            disabled=True,
        )
    return discord.ui.Select(
        placeholder="✕ Remove a secret…",
        min_values=1,
        max_values=1,
        options=[discord.SelectOption(label=f"✕ {key}"[:100], value=key) for key in secret_names],
        disabled=is_system,
    )
