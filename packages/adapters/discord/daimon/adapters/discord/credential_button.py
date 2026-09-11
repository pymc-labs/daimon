"""CredentialRequestButton -- persistent, per-request credential button.

The agent posts this button from the MCP process (Cloud Run); the click is
dispatched to the Discord bot process (worker VM) — a different Python
process entirely. Discord routes interactions by bot application id, not by
which process sent the message, so both processes only need to authenticate
with the same bot token; no other coordination is required.

A per-request button cannot be served by a single long-lived View instance
registered with `bot.add_view()` — that call only registers one already-built
instance's fixed custom_id, and this button's custom_id is minted fresh per
request. `discord.ui.DynamicItem` is the primitive that matches a *template*
regex against an arbitrary future custom_id and reconstructs per-click state
via `from_custom_id`, so this is the one place in the Discord adapter that
carries a persistent, dispatchable custom_id — everywhere else stays
non-persistent (see `views.py`'s module docstring for the documented
exception).

Authorization is requester-only for the env and MCP kinds: `interaction_check`
compares the clicking user's id against the id the request was minted for,
and there is deliberately NO admin gate for either — the setup panel's
mutation buttons are admin-only, but this flow intentionally widens that to
"whoever the agent was talking to" for one-click UX. Do not "restore" an
admin check for either of those two kinds without a deliberate decision to
change that.

The repo kind is the deliberate exception: a repo binding changes what code
the agent clones and runs and, on a shared agent, reaches every member of
the install, so it is additionally gated on
`credential_repo_bind.refuse_if_shared_and_not_admin_for_request` — run as a
pre-filter in `callback` below, before the modal even opens, and again in
`RepoBindModal.on_submit` before the atomic consume. `interaction_check`
itself runs the same requester/expiry/single-use check for every kind,
including this one; it carries no admin logic of its own.

The repo kind's non-admin branch bounds that pre-filter with
`_PRE_FILTER_TIMEOUT_SECONDS` and opens the modal anyway if it times out.
That is sound only because `RepoBindModal.on_submit` repeats the identical
gate before the consume — this pre-filter is advisory, not authoritative, so
falling through never grants a write the submit-time check would not also
grant. What a timeout costs is `T-18-18` reverting to its pre-revision shape
for that one click: a member who will still be refused briefly sees the
token field before the submit-time gate turns them away. Never confuse
`is_done()`, used below to pick which half of the response already fired,
with the actual defence against an interaction acknowledged while this
pre-filter was running: `is_done()` only reflects an ack whose POST had
already returned by the time a cancellation lands, and a POST that is still
outstanding leaves it False. The `discord.HTTPException` code-40060 catch is
what covers that outstanding-POST window instead.

discord.py's own dispatcher swallows every exception raised inside
`from_custom_id`, `interaction_check`, and `callback` (logs internally and
returns) — verified against the installed `discord/ui/view.py`. That makes
those three methods a genuine adapter boundary: the ordinary
let-failures-propagate rule does not apply here, because propagating would
mean the exception vanishes into a dispatcher we don't control and the user
sees nothing but a stuck interaction. Each method below catches narrowly (or,
for the check/callback bodies, catches broadly with a structlog record and an
ephemeral reply) specifically because this is the last point before that
swallowing dispatcher.
"""

from __future__ import annotations

import asyncio
import re
from datetime import UTC, datetime
from typing import Any, Final, Self, cast

import structlog
from daimon.adapters.discord.bot import DaimonBot
from daimon.adapters.discord.checks import is_guild_admin
from daimon.adapters.discord.credential_modals import (
    EnvCredentialModal,
    McpCredentialModal,
    RepoBindModal,
    SkillRepoModal,
)
from daimon.adapters.discord.credential_repo_bind import (
    refuse_if_shared_and_not_admin_for_request,
)
from daimon.adapters.discord.runtime import DiscordRuntime
from daimon.core.credential_requests import (
    CUSTOM_ID_TEMPLATE,
    CredentialRequestKind,
    build_button_label,
    build_custom_id,
)
from daimon.core.stores.credential_requests import peek_credential_request
from daimon.core.stores.domain import CredentialRequestRow
from sqlalchemy.exc import SQLAlchemyError

import discord
from discord.ext import commands

_log = structlog.get_logger()

# Shown when the lookup in from_custom_id found nothing (unknown token or a
# DB failure) — the real label (naming the exact target) only exists once a
# row is found, so this is the best available fallback for a dead button.
_FALLBACK_LABEL = "Enter it privately"

_NO_LONGER_VALID = "This request is no longer valid — ask again."
_WRONG_REQUESTER = "This request was for someone else — ask again in your own thread."
_EXPIRED = "This request expired — ask again."
_ALREADY_USED = "This request was already used — ask again."
_CHECK_FAILED = "Something went wrong checking this request — please try again."
_CALLBACK_FAILED = "Something went wrong opening this form — please try again."

_PRE_FILTER_TIMEOUT_SECONDS: Final[float] = 1.5
"""Bound on the repo kind's non-admin pre-filter, measured from entry into
`callback` rather than from interaction creation — so it can still overshoot
the real 3-second ack budget; that is strictly better than no bound at all,
and never unsafe (see the module docstring for why falling through on expiry
is sound). Chosen against that 3-second budget, leaving room for
`send_modal`'s own round trip and for whatever `from_custom_id` already
spent reading the row. The underlying read
(`refuse_if_shared_and_not_admin_for_request` -> `list_agents_by_tenant`)
paginates the operator's entire MA org, so it costs the same on every click
regardless of this bound; a retry after a timeout fails identically, which
is why the fallback here is to open the modal rather than to ask the member
to click again."""


class CredentialRequestButton(
    discord.ui.DynamicItem[discord.ui.Button[discord.ui.View]], template=CUSTOM_ID_TEMPLATE
):
    """Persistent, per-request credential button reconstructed from its custom_id.

    Registered ONCE as a class (not per-button) via `client.add_dynamic_items`
    in `bot.py`'s `setup_hook`. See the module docstring for why authorization
    here is requester-only with no admin gate, and why the three dispatched
    methods below catch exceptions themselves rather than letting them
    propagate.
    """

    def __init__(self, *, token: str, label: str, request_row: CredentialRequestRow | None) -> None:
        button: discord.ui.Button[discord.ui.View] = discord.ui.Button(
            style=discord.ButtonStyle.primary,
            label=label,
            custom_id=build_custom_id(token),
        )
        super().__init__(button)
        self.token = token
        self.request_row = request_row

    @classmethod
    async def from_custom_id(  # type: ignore[override]  # discord.py's ClientT is a free TypeVar; this adapter only ever runs DaimonBot
        cls,
        interaction: discord.Interaction[commands.Bot],
        item: discord.ui.Item[Any],
        match: re.Match[str],
        /,
    ) -> Self:
        """Rebuild the item from its token. Runs before the 3s ack budget starts.

        A single indexed primary-key read — no MA calls, no extra queries.
        Never raises: discord.py logs and discards any exception from this
        method, so a raised DB error here would leave the user with a
        permanently stuck interaction and nothing in our own logs.
        """
        token = match["token"]
        bot = cast(DaimonBot, interaction.client)
        try:
            async with bot.runtime.sessionmaker() as session:
                request_row = await peek_credential_request(session, token=token)
        except SQLAlchemyError:
            _log.exception("credential_button.lookup_failed", token_tail=token[-4:])
            request_row = None
        if request_row is None:
            return cls(token=token, label=_FALLBACK_LABEL, request_row=None)
        # `CredentialRequestRow.kind` is a bare `str` at the store boundary (ORM ->
        # Pydantic mapping); every row is inserted with a validated
        # `CredentialRequestKind` (daimon.core.credential_requests.create_*), so this
        # narrowing reflects a real invariant rather than papering over one.
        kind = cast(CredentialRequestKind, request_row.kind)
        label = build_button_label(kind, request_row.target)
        return cls(token=token, label=label, request_row=request_row)

    async def interaction_check(  # type: ignore[override]  # see from_custom_id
        self, interaction: discord.Interaction[commands.Bot], /
    ) -> bool:
        """Requester/expiry/single-use check, common to every kind.

        This is fast user feedback, not the security boundary, for every
        kind — the authoritative single-use gate is the atomic consume
        inside the modal's `on_submit`; rejecting an already-used or expired
        row here just saves the round trip. This method carries no admin
        logic of its own: the repo kind's additional shared-agent admin gate
        lives in `callback`'s pre-filter and in `RepoBindModal.on_submit`,
        not here.
        """
        try:
            request_row = self.request_row
            if request_row is None:
                await interaction.response.send_message(_NO_LONGER_VALID, ephemeral=True)
                return False
            if str(interaction.user.id) != request_row.requester_platform_user_id:
                await interaction.response.send_message(_WRONG_REQUESTER, ephemeral=True)
                return False
            if request_row.expires_at < datetime.now(UTC):
                await interaction.response.send_message(_EXPIRED, ephemeral=True)
                return False
            if request_row.used_at is not None:
                await interaction.response.send_message(_ALREADY_USED, ephemeral=True)
                return False
            return True
        except Exception as err:  # noqa: BLE001 -- dynamic-item dispatch is an adapter boundary (see module docstring); discord.py's own dispatcher swallows anything raised here
            _log.exception(
                "credential_button.interaction_check_failed", err_type=type(err).__name__
            )
            await interaction.response.send_message(_CHECK_FAILED, ephemeral=True)
            return False

    async def callback(  # type: ignore[override]  # see from_custom_id
        self, interaction: discord.Interaction[commands.Bot]
    ) -> None:
        """Open the modal matching this request's kind.

        The repo kind runs a non-admin pre-filter before the modal opens —
        see the module docstring and `_PRE_FILTER_TIMEOUT_SECONDS`. Never
        defers: opening a modal requires an un-acked interaction, so every
        response below is the interaction's first (and, on the repo kind's
        refusal path, only) response.
        """
        try:
            request_row = self.request_row
            if request_row is None:
                return
            bot = cast(DaimonBot, interaction.client)
            if request_row.kind == "env":
                await interaction.response.send_modal(
                    EnvCredentialModal(runtime=bot.runtime, request_row=request_row)
                )
            elif request_row.kind == "repo":
                await self._open_repo_modal(interaction, runtime=bot.runtime, row=request_row)
            elif request_row.kind == "skill_repo":
                # Explicit branch, not a fall-through: the `else` below is the
                # MCP modal, so a kind added without a branch here silently
                # opens the wrong modal rather than failing.
                #
                # No admin pre-filter, unlike the repo kind — this writes no
                # agent_repo_binding, so the "changes what code the agent runs
                # for every member" reasoning behind that gate does not apply.
                await interaction.response.send_modal(
                    SkillRepoModal(runtime=bot.runtime, request_row=request_row)
                )
            else:
                await interaction.response.send_modal(
                    McpCredentialModal(runtime=bot.runtime, request_row=request_row)
                )
        except Exception as err:  # noqa: BLE001 -- dynamic-item dispatch is an adapter boundary (see module docstring); discord.py's own dispatcher swallows anything raised here
            _log.exception("credential_button.callback_failed", err_type=type(err).__name__)
            await interaction.response.send_message(_CALLBACK_FAILED, ephemeral=True)

    async def _open_repo_modal(
        self,
        interaction: discord.Interaction[commands.Bot],
        *,
        runtime: DiscordRuntime,
        row: CredentialRequestRow,
    ) -> None:
        """The repo kind's dispatch arm: an admin pre-filter, then the modal.

        A live guild admin (`is_guild_admin`, zero I/O) reaches `send_modal`
        immediately. A non-admin instead runs the full
        `refuse_if_shared_and_not_admin_for_request` gate, bounded by
        `_PRE_FILTER_TIMEOUT_SECONDS` — see the module docstring for why
        falling through to `send_modal` on a timeout is sound.
        """
        if is_guild_admin(interaction):
            await interaction.response.send_modal(RepoBindModal(runtime=runtime, request_row=row))
            return
        try:
            refused = await asyncio.wait_for(
                refuse_if_shared_and_not_admin_for_request(
                    interaction,
                    runtime=runtime,
                    tenant_id=row.tenant_id,
                    agent_id=row.agent_id,
                ),
                timeout=_PRE_FILTER_TIMEOUT_SECONDS,
            )
        except TimeoutError:
            _log.warning(
                "credential_button.repo_pre_filter_timed_out",
                tenant_id=str(row.tenant_id),
                timeout_seconds=_PRE_FILTER_TIMEOUT_SECONDS,
            )
            if interaction.response.is_done():
                return
            try:
                await interaction.response.send_modal(
                    RepoBindModal(runtime=runtime, request_row=row)
                )
            except discord.HTTPException as http_err:
                if http_err.code == 40060:
                    _log.debug(
                        "credential_button.repo_pre_filter_already_acked",
                        tenant_id=str(row.tenant_id),
                    )
                    return
                raise
            return
        if refused:
            return
        await interaction.response.send_modal(RepoBindModal(runtime=runtime, request_row=row))
