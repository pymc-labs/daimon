"""The private forms a posted control's button opens.

Five separate forms, not one type-dispatching form: an env secret, a whole
`.env` file, an MCP auth token, a skill repo and a repo binding are different
resources with different write paths (`put_agent_file_if_unchanged` vs
`add_external_mcp_credential` vs `set_skill_repo_credential` vs
`agent_repo_binding.set_binding`), mirroring the forms that already exist
for the same writes on the setup panel (`agent_setup/credentials.py`'s
`PasteSecretModal`, `agent_setup/modals_mcp.py`'s `AddMcpModal`,
`agent_setup/modals.py`'s `RepoAuthModal`).

Every one of them asks for exactly ONE thing, above a `TextDisplay` line that
restates the facts the card already showed. Nothing else is asked, because
every routing field — the agent, the key name, the server name, the repo and
its branch — is already fixed by the consumed `credential_requests` row and
must never be retyped: what the person holds and the card does not is the
value alone. The one input is wrapped in a `discord.ui.Label`, which is where
its visible text and helper line live (`TextInput.label` is deprecated in
discord.py 2.6+), and the title is built by `_title` so it fits Discord's
45-character cap whatever the key name or repo is called.

Secret hygiene, matching the structural guarantees `PasteSecretModal` already
documents:
- the value never reaches a log record (env logs the key name only; MCP and
  repo log a masked tail only),
- the value never reaches a `custom_id` (the button's custom_id carries only
  the opaque request token, minted before any modal exists),
- the value never reaches a container/embed (a form input has no render
  surface other than the ephemeral confirmation, which never echoes the
  value back),
- there is no URL-fetch path, and the one upload path (`EnvFileModal`)
  keeps no copy of the file and never puts a parsed value in a message, a
  log record or a rejection — `EnvProblem` has no field for one.

The atomic single-use consume runs BEFORE every write, so a request can
only ever produce one write no matter how many times its modal is
(re)submitted — the loser of a race, or any resubmission, gets `None` back
and writes nothing.

Every `on_submit` acks with a bare `defer()`, never `thinking=True`. On a
modal opened from a component click that is a `deferred_message_update`,
whose `@original` is the message the button lives on. `thinking=True` would
point `@original` at a fresh ephemeral instead, the defect
`agent_setup/credentials.py` already fixed on the setup panel. Ephemeral
followups still work after this ack, so every validation and error toast
below is unchanged. There is no success toast: the posted card IS the
receipt, and a second copy of it in the thread says the same thing twice.

That card is re-rendered by message id
(`posted_controls.edit_posted_card`), not through `@original`. It moves to
its `received` state the moment the consume commits — before the
vault/credential/import below is known to have worked — and then to the
state that actually happened: `applied`, `partial` when the value was
stored but the work it enables did not finish, `refused` when the
submit-time gate turned the write away, or `superseded` when the value the
card promised to replace had already changed underneath it. No card ever
claims more than the write it is reporting.

Whatever the outcome, the spent request records it
(`set_credential_request_outcome`), and every write that actually landed
also queues the one turn it owes (`record_input_continuation`) in the same
transaction as that record — so a value can never land without its
follow-up, nor a follow-up without its value.
The follow-up turn is never awaited here: `_dispatch_origin_thread` spawns
it on the bot, because a billed turn must not sit inside a Discord
interaction.

`EnvFileModal` is the one form that takes a file rather than typed text. It
caps on `Attachment.size` BEFORE downloading, parses the whole file or
rejects the whole file (`daimon.core.env_file`), and only then consumes the
request: a typo in an uploaded file must not burn the one click the person
gets. Its write is all-or-nothing inside a savepoint, and it sends no success
ephemeral — the card it edits into `applied` is the receipt.

`RepoBindModal`'s write is additionally admin-gated on a shared agent: token
consumption alone is sufficient authorization for an env secret or an MCP
token (the requester supplies a value only they hold, scoped to one key on
one agent), but a repo binding changes what code the agent clones and runs
and, on a shared agent, reaches every member of the install. So its
`on_submit` also runs
`credential_repo_bind.refuse_if_shared_and_not_admin_for_request` — once
here, right after `defer` and before the consume, and once more as a
pre-filter in `credential_button.py`'s `callback`, before the modal is even
opened.
"""

from __future__ import annotations

import uuid
from collections.abc import Collection, Sequence
from datetime import UTC, datetime
from typing import Final, cast

import anthropic
import httpx
import structlog
from anthropic.types.beta import BetaManagedAgentsAgent
from anthropic.types.beta.beta_managed_agents_skill_params import BetaManagedAgentsSkillParams
from daimon.adapters.discord.bot import DaimonBot
from daimon.adapters.discord.checks import channel_admin_caller, is_guild_admin
from daimon.adapters.discord.credential_origin import (
    is_credential_interaction_valid,
    resolve_credential_target,
)
from daimon.adapters.discord.credential_repo_bind import (
    refuse_if_shared_and_not_admin_for_request,
    resolve_ma_agent_for_uuid,
    resolve_repo_binding_credential,
)
from daimon.adapters.discord.posted_controls import edit_posted_card
from daimon.adapters.discord.runtime import DiscordRuntime
from daimon.core.agent_pins import FormPinRefused, agent_pin_names
from daimon.core.agent_reach import load_target_facts
from daimon.core.constants import MAX_SECRET_VALUE_BYTES
from daimon.core.continuity.messages import (
    ConfigurationChange,
    render_env_import_rejected,
)
from daimon.core.credential_requests import (
    CredentialRequestOutcome,
    availability_for_request,
    split_skill_repo_target,
)
from daimon.core.credential_submit import (
    apply_env_file_submit,
    apply_env_submit,
    consume_credential_submit,
    prepare_env_submit,
    prepare_mcp_submit,
    settle_credential_submit,
    write_mcp_submit,
    write_repo_submit,
    write_skill_repo_submit,
)
from daimon.core.credential_submit import env_name_refusal as _env_name_refusal
from daimon.core.defaults.ma_index import find_agent_by_derived_uuid, find_attach_mount_collision
from daimon.core.defaults.metadata import MA_METADATA_KEY_MANAGED
from daimon.core.defaults.report import ResourceOutcome
from daimon.core.defaults.spec_merge import merge_skills_with_ma
from daimon.core.env_file import (
    MAX_ENV_FILE_BYTES,
    EnvEntry,
    EnvFileRejected,
    decode_env_bytes,
    env_collision_line,
    env_name_problem,
    parse_env_file,
)
from daimon.core.errors import DaimonError
from daimon.core.github_repo_auth import normalize_owner_repo
from daimon.core.github_visibility import pat_can_access_repo
from daimon.core.ma import update_agent_with_version_retry
from daimon.core.mcp_attach import (
    McpConnectDecision,
    McpServerReplaceRefusedError,
    decide_mcp_connect,
)
from daimon.core.mcp_token_check import is_token_rejected, rejected_token_message
from daimon.core.mcp_token_connect import (
    McpAgentGoneError,
    McpAttachFailedError,
    McpTokenWriteFailedError,
    connect_mcp_server_with_token,
)
from daimon.core.operation_policy import PolicyOutcome, decide_operation
from daimon.core.posted_controls import (
    NO_LONGER_VALID_MESSAGE,
    CardState,
    RefusalReason,
    build_posted_card,
    card_text,
)
from daimon.core.skills.pipeline import run_skill_sync
from daimon.core.skills.sync import in_library, library_skill_ids, summarize_failed_imports
from daimon.core.stores import credential_requests
from daimon.core.stores.agent_files import (
    AgentEnvEncryptionRequiredError,
)
from daimon.core.stores.agent_repo_binding import set_binding
from daimon.core.stores.domain import CredentialRequestRow

import discord

_log = structlog.get_logger()

_NO_LONGER_VALID = NO_LONGER_VALID_MESSAGE

#: Discord rejects a form whose title is longer than this.
_MAX_TITLE_CHARS: Final[int] = 45

#: A row minted before its agent was resolved names no target, and one
#: minted outside a turn names no responder — same fallbacks the card's own
#: renderer (`posted_controls.edit`) uses, because both read the same row.
_UNNAMED_AGENT: Final[str] = "the agent"
_UNNAMED_RESPONDER: Final[str] = "Daimon"

#: How many colliding keys the refusal names before it summarises the rest,
#: matching what `render_env_import_rejected` shows for a rejected file.
_COLLISIONS_SHOWN: Final[int] = 3


def _title(target: str, agent: str, fallback: str) -> str:
    """Name the form after what it is for, inside Discord's 45-character cap.

    The pair reads best ("TOGGL_TOKEN for research-bot"), so it is tried
    first; a long agent name costs the agent, and a target too long to show
    at all falls back to the generic name for that kind of form.
    """
    paired = f"{target} for {agent}"
    if len(paired) <= _MAX_TITLE_CHARS:
        return paired
    if len(target) <= _MAX_TITLE_CHARS:
        return target
    return fallback


def _agent_name(row: CredentialRequestRow) -> str:
    """The agent this request names, as the person sees it on the card."""
    return row.target_name or _UNNAMED_AGENT


def _text_input_of[ModalT: discord.ui.Modal](
    label: discord.ui.Label[ModalT],
) -> discord.ui.TextInput[ModalT]:
    """Return the `TextInput` a form just wrapped in `label`.

    `Label.component` is typed `Item` because a label may wrap any input;
    every call below builds its own `TextInput` inline (which is also what
    lets `scripts/lint_discord_modals.py` see the two as one component), so
    the narrowing is of a fact the caller established one line earlier.
    """
    return cast(discord.ui.TextInput[ModalT], label.component)


def _env_card_text(
    row: CredentialRequestRow,
    *,
    state: CardState,
    refusal: RefusalReason | None = None,
    replaces: str | None = None,
) -> str:
    """The words the env card itself now carries, for the ephemeral to repeat.

    A refusal and a superseded replacement are the two outcomes the
    submitter needs in their own reply as well as on the card; writing them
    twice is how the two drift, so both come from the one card builder.
    """
    return card_text(
        build_posted_card(
            kind="env",
            state=state,
            agent_name=_agent_name(row),
            responder_name=row.responder_name or _UNNAMED_RESPONDER,
            target=row.target,
            requester_platform_user_id=row.requester_platform_user_id,
            expires_at=row.expires_at,
            token=row.token,
            refusal=refusal,
            replaces=replaces,
        )
    )


async def _settle_spent_request(
    runtime: DiscordRuntime,
    *,
    row: CredentialRequestRow,
    outcome: CredentialRequestOutcome,
    carries_work: bool,
) -> bool:
    """Record how a spent request ended and queue its continuation, together.

    For the three forms whose value lands outside our database (a vault
    credential, an MA attach, a skill import) this is the one transaction
    that can hold both facts. The two env forms write theirs inside the
    transaction that already carries their value write.
    """
    return await settle_credential_submit(
        runtime.sessionmaker,
        row=row,
        platform="discord",
        outcome=outcome,
        carries_work=carries_work,
    )


async def _record_refused_outcome(runtime: DiscordRuntime, row: CredentialRequestRow) -> None:
    """Record a refusal that consumed nothing.

    Deliberately queues no continuation: the request is still unspent, and a
    continuation row would claim its `idempotency_key`, so a later
    submission of the same still-live token could not queue its own.
    """
    async with runtime.sessionmaker.begin() as session:
        await credential_requests.set_credential_request_outcome(
            session, token=row.token, outcome="write_failed"
        )


async def _refuse_for_unavailable_target(
    runtime: DiscordRuntime, interaction: discord.Interaction, row: CredentialRequestRow
) -> None:
    """Close a SPENT request whose target turned out to be unusable.

    Unlike `_record_refused_outcome`, whose request is still live, the consume
    has already happened here — so leaving the card on `received` would have it
    say "Saving…" about a save that stopped. Nothing is known to have reached a
    store on this path, so the terminal state is `refused` rather than
    `partial`, and no continuation is queued: work waiting on this value must
    not resume on a value that never landed.
    """
    async with runtime.sessionmaker.begin() as session:
        await credential_requests.set_credential_request_outcome(
            session, token=row.token, outcome="write_failed"
        )
    await edit_posted_card(
        interaction.client, row=row, state="refused", refusal="target_unavailable"
    )


async def _refuse_for_rejected_token(
    runtime: DiscordRuntime, interaction: discord.Interaction, row: CredentialRequestRow
) -> None:
    """Close a SPENT request whose token the server refused before any write."""
    async with runtime.sessionmaker.begin() as session:
        await credential_requests.set_credential_request_outcome(
            session, token=row.token, outcome="token_rejected"
        )
    await edit_posted_card(interaction.client, row=row, state="refused", refusal="token_rejected")


async def _dispatch_origin_thread(
    interaction: discord.Interaction, row: CredentialRequestRow
) -> None:
    """Kick the origin thread's queued continuations without blocking this form.

    The follow-up is a billed turn posted into a thread people are reading,
    so it is spawned rather than awaited: an interaction that waited for it
    would be long dead before the turn finished. A failure inside the
    spawned task is logged by the bot's own task callback and costs nothing
    — the row stays pending, and the next completed turn in that thread
    picks it up.
    """
    if row.origin_thread_id is None or interaction.guild_id is None:
        return
    bot = cast(DaimonBot, interaction.client)
    try:
        thread = bot.get_channel(int(row.origin_thread_id)) or await bot.fetch_channel(
            int(row.origin_thread_id)
        )
    except discord.HTTPException:
        _log.warning("credential_modal.origin_thread_unreachable", thread_id=row.origin_thread_id)
        return
    if not isinstance(thread, discord.Thread):
        return
    bot._spawn(  # pyright: ignore[reportPrivateUsage]  # DaimonBot's tracked fire-and-forget helper; the billed follow-up must never block this interaction
        bot.dispatch_continuations_in_thread(
            tenant_id=row.tenant_id, thread=thread, guild_id=str(interaction.guild_id)
        )
    )


async def _decide_key_replacement(
    interaction: discord.Interaction, *, runtime: DiscordRuntime, row: CredentialRequestRow
) -> PolicyOutcome:
    """Re-decide a key REPLACEMENT's authorization, at submit time.

    Adding a key needs no gate at all (`decide_operation` always allows
    `key_add`: a new value overwrites nothing). Replacing one overwrites a
    value the whole install may be running on, so it is the attachment
    family's decision instead — and it is taken again here, because the
    person who was an admin, or whose target was private, when the card was
    posted may be neither by the time they submit.

    Same facts in the same order as
    `credential_repo_bind.refuse_if_shared_and_not_admin_for_request`: live
    guild admin first (no I/O), then the resolved agent's defaults-managed
    flag, then a fresh reachability read — and every one of them read before
    the consume's transaction opens, so no row lock is ever held across an
    MA listing.
    """
    if is_guild_admin(interaction):  # pyright: ignore[reportArgumentType]  # discord.Interaction vs Interaction[commands.Bot]; is_guild_admin only reads user/guild
        return "allow"
    agent = await resolve_ma_agent_for_uuid(
        runtime.anthropic, tenant_id=row.tenant_id, agent_id=row.agent_id
    )
    if agent is None:
        # Fail closed: the target this replacement was authorized against is
        # gone, so nothing can establish that it is not shared.
        return "needs_admin"
    is_daimon_managed = agent.metadata.get(MA_METADATA_KEY_MANAGED) == "true"
    async with runtime.sessionmaker() as session:
        facts = await load_target_facts(
            session,
            "key_replace",
            tenant_id=row.tenant_id,
            platform="discord",
            agent_names=agent_pin_names(agent.name, agent.metadata),
            ma_agent_id=str(agent.id),
            default=runtime.deployment_default,
            caller=channel_admin_caller(interaction.user),
            is_daimon_managed=is_daimon_managed,
            caller_account_id=row.account_id,
            caller_platform_user_id=row.requester_platform_user_id,
        )
    return decide_operation(
        "key_replace",
        is_admin=False,
        target=facts,
    )


async def _decide_mcp_connect_at_submit(
    interaction: discord.Interaction, *, runtime: DiscordRuntime, row: CredentialRequestRow
) -> McpConnectDecision:
    """Re-decide an MCP token submission against the person submitting it.

    Repointing a server the agent already has, or overwriting the agent-wide
    token for its URL, is an `mcp_replace` attachment write: on a shared agent
    it needs a live guild admin. Read before the consume, like a key
    replacement, so no row lock is held across the MA listing. A target that
    cannot be resolved is left to the unavailable-target path after the
    consume, and nothing may be replaced for it.
    """
    agent = await find_agent_by_derived_uuid(
        runtime.anthropic, tenant_id=row.tenant_id, agent_id=row.agent_id
    )
    if agent is None or row.mcp_server_url is None:
        return McpConnectDecision(replaces=False, replace_allowed=False)
    return await decide_mcp_connect(
        runtime.sessionmaker,
        tenant_id=row.tenant_id,
        agent=agent,
        agent_id=row.agent_id,
        server_name=row.target,
        url=row.mcp_server_url,
        platform="discord",
        caller=channel_admin_caller(interaction.user).model_copy(
            update={"is_server_admin": is_guild_admin(interaction)}  # pyright: ignore[reportArgumentType]  # discord.Interaction vs Interaction[commands.Bot]; is_guild_admin only reads user/guild
        ),
        default=runtime.deployment_default,
        shares_token=True,
    )


class EnvCredentialModal(discord.ui.Modal):
    """Add one key: one value, atomic consume, existing agent_files write."""

    def __init__(self, *, runtime: DiscordRuntime, request_row: CredentialRequestRow) -> None:
        agent = _agent_name(request_row)
        super().__init__(title=_title(request_row.target, agent, "Add a key"))
        self._runtime = runtime
        self._row = request_row
        self.add_item(
            discord.ui.TextDisplay(
                f"-# Anyone who talks to {agent} can use it. The value is not shown in chat."
            )
        )
        value_label: discord.ui.Label[EnvCredentialModal] = discord.ui.Label(
            text="Value",
            component=discord.ui.TextInput(
                style=discord.TextStyle.paragraph,
                required=True,
                max_length=4000,
                placeholder="paste the value",
            ),
        )
        self.value_input = _text_input_of(value_label)
        self.add_item(value_label)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        if not is_credential_interaction_valid(interaction, self._row):
            await interaction.followup.send(_NO_LONGER_VALID, ephemeral=True)
            return
        raw_value = str(self.value_input.value or "")

        if not raw_value.strip():
            await interaction.followup.send(
                "Key value cannot be empty — try again.", ephemeral=True
            )
            return
        if len(raw_value.encode()) > MAX_SECRET_VALUE_BYTES:
            await interaction.followup.send(
                f"Key value is too large. Max {MAX_SECRET_VALUE_BYTES} bytes.",
                ephemeral=True,
            )
            return

        agent = await resolve_credential_target(interaction, runtime=self._runtime, row=self._row)
        if agent is None:
            return
        # The name a member may store is re-checked against the submitter's
        # live role: a key minted while they were an admin must not land as a
        # member. `key_add` itself needs no gate, but the NAME does.
        is_admin = is_guild_admin(interaction)  # pyright: ignore[reportArgumentType]
        name_problem = env_name_problem(self._row.target, is_admin=is_admin)
        if name_problem is not None:
            await interaction.followup.send(
                _env_name_refusal(self._row.target, name_problem), ephemeral=True
            )
            return

        async def replacement_refused() -> bool:
            return (
                await _decide_key_replacement(interaction, runtime=self._runtime, row=self._row)
            ) != "allow"

        plan = await prepare_env_submit(
            self._runtime.sessionmaker, row=self._row, replacement_refused=replacement_refused
        )
        shadowed = plan.shadowed
        try:
            result = await apply_env_submit(
                self._runtime.sessionmaker,
                row=self._row,
                agent=agent,
                platform="discord",
                value=raw_value,
                plan=plan,
                now=datetime.now(UTC),
            )
            consumed_row = result.consumed
            if consumed_row is None:
                await interaction.followup.send(_NO_LONGER_VALID, ephemeral=True)
                return
            state, outcome = result.state, result.outcome
            is_continuation_queued = result.queued
        except FormPinRefused as refused:
            # Decided with the consume: rolled back, nothing stored.
            await interaction.followup.send(refused.refusal, ephemeral=True)
            return
        except AgentEnvEncryptionRequiredError as err:
            # Rolled back with the consume: nothing stored, the request stays live.
            _log.error("credential_modal.env_write_refused_no_crypto_keys")
            await interaction.followup.send(str(err), ephemeral=True)
            return
        except Exception:
            _log.exception("credential_modal.env_write_failed", key=self._row.target)
            await interaction.followup.send(
                "Something went wrong — please try again.", ephemeral=True
            )
            return

        # Log the key NAME only — never the value.
        _log.info("credential_modal.env.submit", key=consumed_row.target, outcome=outcome)
        # After the transaction, not inside it: the consume, the file write and
        # the continuation commit together here, so this is the first point the
        # row is durably spent, and it keeps a Discord round trip out of an open
        # transaction. The `received` edit still runs first: the terminal edit
        # below can fail, and a card left saying "Saving…" is a better last
        # state than one still offering a button that can only be refused.
        await edit_posted_card(interaction.client, row=consumed_row, state="received")
        if state == "refused":
            await edit_posted_card(
                interaction.client,
                row=consumed_row,
                state="refused",
                refusal="replacement_admin_required",
                replaces=shadowed,
            )
            await interaction.followup.send(
                _env_card_text(
                    consumed_row,
                    state="refused",
                    refusal="replacement_admin_required",
                    replaces=shadowed,
                ),
                ephemeral=True,
            )
            return
        if state == "superseded":
            await edit_posted_card(interaction.client, row=consumed_row, state="superseded")
            await interaction.followup.send(
                _env_card_text(consumed_row, state="superseded"), ephemeral=True
            )
            return
        await edit_posted_card(
            interaction.client,
            row=consumed_row,
            state="applied",
            outcome=ConfigurationChange(
                target_name=_agent_name(consumed_row),
                kind="key",
                availability=availability_for_request(consumed_row),
                detail=consumed_row.target,
            ),
        )
        if is_continuation_queued:
            await _dispatch_origin_thread(interaction, consumed_row)


def _collision_lines(collisions: Sequence[EnvEntry], held: Collection[str]) -> tuple[str, ...]:
    """Name the keys the file would have overwritten, by name and line only.

    A value never reaches this copy: the card these lines land on is public
    to the channel, so the only facts it may carry are the ones already on
    the uploader's screen.
    """
    lines = [env_collision_line(entry, held) for entry in collisions]
    shown = lines[:_COLLISIONS_SHOWN]
    remaining = len(lines) - _COLLISIONS_SHOWN
    if remaining > 0:
        shown.append(f"…and {remaining} more.")
    shown.append("Nothing was changed. Take them out of the file, or ask to replace them by name.")
    return tuple(shown)


class EnvFileModal(discord.ui.Modal):
    """Add every key in an uploaded `.env` file, or none of them.

    The whole file is parsed before the request is consumed, so a syntax
    error or a duplicate name costs nothing but a re-upload. Once the
    request is spent the keys land together inside one savepoint: a key that
    already exists — including one that appears between the read and the
    write — refuses the whole file rather than merging half of it, because a
    silently half-applied secrets file is the failure nobody notices.
    """

    def __init__(self, *, runtime: DiscordRuntime, request_row: CredentialRequestRow) -> None:
        agent = _agent_name(request_row)
        super().__init__(title=_title(request_row.target, agent, "Keys from a file"))
        self._runtime = runtime
        self._row = request_row
        self.add_item(
            discord.ui.TextDisplay(
                "-# One KEY=VALUE per line. Daimon stores the keys, not a retained copy of "
                "your uploaded file."
            )
        )
        upload_label: discord.ui.Label[EnvFileModal] = discord.ui.Label(
            text=".env file",
            component=discord.ui.FileUpload(required=True, min_values=1, max_values=1),
        )
        # `Label.component` is typed `Item`; this one was built as the
        # FileUpload on the line above (see `_text_input_of`).
        self.file_input = cast("discord.ui.FileUpload[EnvFileModal]", upload_label.component)
        self.add_item(upload_label)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        if not is_credential_interaction_valid(interaction, self._row):
            await interaction.followup.send(_NO_LONGER_VALID, ephemeral=True)
            return
        agent_name = _agent_name(self._row)

        uploads = self.file_input.values
        if not uploads:
            await interaction.followup.send(
                f"No file arrived. Upload a .env file to add keys to {agent_name}.",
                ephemeral=True,
            )
            return
        upload = uploads[0]
        # Cap on the announced size BEFORE downloading: `decode_env_bytes`
        # enforces the same bound, but only once the bytes are already here.
        if upload.size > MAX_ENV_FILE_BYTES:
            await interaction.followup.send(
                render_env_import_rejected("file_too_large", (), target_name=agent_name),
                ephemeral=True,
            )
            return
        try:
            raw = await upload.read()
        except discord.HTTPException:
            _log.warning("credential_modal.env_file_download_failed", size=upload.size)
            await interaction.followup.send(
                "I could not read that file. Upload it again.", ephemeral=True
            )
            return
        try:
            entries = parse_env_file(
                decode_env_bytes(raw),
                member_writable_only=not is_guild_admin(interaction),  # pyright: ignore[reportArgumentType]
            )
        except EnvFileRejected as rejected:
            # The request is deliberately NOT consumed: a typo in the file
            # must not cost the one click this card is good for.
            await interaction.followup.send(
                render_env_import_rejected(
                    rejected.rejection, rejected.problems, target_name=agent_name
                ),
                ephemeral=True,
            )
            return

        agent = await resolve_credential_target(interaction, runtime=self._runtime, row=self._row)
        if agent is None:
            return

        try:
            consumed_row, collisions, is_continuation_queued, held = await apply_env_file_submit(
                self._runtime.sessionmaker,
                row=self._row,
                agent=agent,
                platform="discord",
                entries=entries,
                now=datetime.now(UTC),
            )
            if consumed_row is None:
                await interaction.followup.send(_NO_LONGER_VALID, ephemeral=True)
                return
        except FormPinRefused as refused:
            # Decided with the consume: rolled back, nothing stored.
            await interaction.followup.send(refused.refusal, ephemeral=True)
            return
        except AgentEnvEncryptionRequiredError as err:
            # Rolled back with the consume: nothing stored, the request stays live.
            _log.error("credential_modal.env_write_refused_no_crypto_keys")
            await interaction.followup.send(str(err), ephemeral=True)
            return
        except Exception:
            # Named boundary: discord.py swallows whatever escapes on_submit.
            _log.exception("credential_modal.env_file_write_failed", key_count=len(entries))
            await interaction.followup.send(
                "Something went wrong — please try again.", ephemeral=True
            )
            return

        # Key NAMES and counts only — never a value.
        _log.info(
            "credential_modal.env_file.submit",
            key_count=len(entries),
            collision_count=len(collisions),
        )
        if collisions:
            refusal_lines = _collision_lines(collisions, held)
            await edit_posted_card(
                interaction.client,
                row=self._row,
                state="refused",
                refusal="env_file_invalid",
                refusal_lines=refusal_lines,
            )
            await interaction.followup.send(
                "\n".join((f"No keys were saved for {agent_name}.", *refusal_lines)),
                ephemeral=True,
            )
            return

        # No success ephemeral: the card below is the receipt, and a second
        # copy of it in the thread says the same thing twice.
        await edit_posted_card(
            interaction.client,
            row=self._row,
            state="applied",
            outcome=ConfigurationChange(
                target_name=agent_name,
                kind="keys_bulk",
                availability=availability_for_request(consumed_row),
                count=len(entries),
            ),
        )
        if is_continuation_queued:
            await _dispatch_origin_thread(interaction, consumed_row)


class McpCredentialModal(discord.ui.Modal):
    """Connect a server: one token, atomic consume, existing vault write."""

    def __init__(self, *, runtime: DiscordRuntime, request_row: CredentialRequestRow) -> None:
        agent = _agent_name(request_row)
        super().__init__(
            title=_title(
                request_row.target, agent, f"{request_row.target} token"[:_MAX_TITLE_CHARS]
            )
        )
        self._runtime = runtime
        self._row = request_row
        self.add_item(
            discord.ui.TextDisplay(
                f"-# For {request_row.mcp_server_url}. "
                f"Anyone who talks to {agent} can use this connection."
            )
        )
        token_label: discord.ui.Label[McpCredentialModal] = discord.ui.Label(
            text="Token",
            component=discord.ui.TextInput(required=True, max_length=4000),
        )
        self.token_input = _text_input_of(token_label)
        self.add_item(token_label)

    async def _refuse_replacement(
        self, interaction: discord.Interaction, consumed_row: CredentialRequestRow
    ) -> None:
        """Spend the request and write nothing: no vault token, no attach."""
        await _settle_spent_request(
            self._runtime, row=consumed_row, outcome="write_failed", carries_work=False
        )
        await edit_posted_card(
            interaction.client,
            row=consumed_row,
            state="refused",
            refusal="replacement_admin_required",
        )
        await interaction.followup.send(
            f"{_agent_name(consumed_row)} already has `{consumed_row.target}` (or a token "
            "for that URL) and is shared here, so replacing it needs a server admin. "
            "Nothing was saved.",
            ephemeral=True,
        )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        if not is_credential_interaction_valid(interaction, self._row):
            await interaction.followup.send(_NO_LONGER_VALID, ephemeral=True)
            return
        token_value = str(self.token_input.value or "")

        if not token_value.strip():
            await interaction.followup.send(
                "MCP token cannot be empty — try again.", ephemeral=True
            )
            return

        public_url_setting = self._runtime.settings.mcp.public_url
        jwt_secret_setting = self._runtime.settings.mcp.jwt_secret
        if public_url_setting is None or jwt_secret_setting is None:
            await interaction.followup.send(
                "This deployment is not finished being set up. Ask the operator to finish setup, "
                "then try again. Nothing was saved.",
                ephemeral=True,
            )
            return

        agent = await resolve_credential_target(interaction, runtime=self._runtime, row=self._row)
        if agent is None:
            return
        connect, now = await prepare_mcp_submit(
            decide=lambda: _decide_mcp_connect_at_submit(
                interaction, runtime=self._runtime, row=self._row
            ),
            clock=lambda: datetime.now(UTC),
        )
        try:
            consumed_row = await consume_credential_submit(
                self._runtime.sessionmaker, row=self._row, agent=agent, now=now
            )
        except FormPinRefused as refused:
            await interaction.followup.send(refused.refusal, ephemeral=True)
            return
        if consumed_row is None:
            await interaction.followup.send(_NO_LONGER_VALID, ephemeral=True)
            return

        await edit_posted_card(interaction.client, row=consumed_row, state="received")
        if connect.refused:
            await self._refuse_replacement(interaction, consumed_row)
            return

        mcp_server_url = consumed_row.mcp_server_url
        if mcp_server_url is None:
            _log.error(
                "credential_modal.mcp_missing_server_url", agent_id=str(consumed_row.agent_id)
            )
            await _refuse_for_unavailable_target(self._runtime, interaction, consumed_row)
            await interaction.followup.send(
                "This request is missing its server URL — please ask again.", ephemeral=True
            )
            return

        _log.info(
            "credential_modal.mcp.submit",
            mcp_server_url=mcp_server_url,
            token_present=bool(token_value),
        )
        # Ask the server first. A token it rejects would otherwise be stored,
        # mirrored into every caller's vault and attached, and every turn from
        # then on would carry a failed MCP init (#79). A server that cannot be
        # reached is not a verdict: the save proceeds and MA reports later.
        if await is_token_rejected(
            self._runtime.mcp_token_probe, mcp_server_url=mcp_server_url, token=token_value
        ):
            await _refuse_for_rejected_token(self._runtime, interaction, consumed_row)
            await interaction.followup.send(rejected_token_message(mcp_server_url), ephemeral=True)
            return
        try:
            await write_mcp_submit(
                self._runtime.anthropic,
                session_factory=self._runtime.sessionmaker,
                row=consumed_row,
                fernet=self._runtime.turn_deps.fernet,
                value=token_value,
                replace_allowed=connect.replace_allowed,
                jwt_secret=jwt_secret_setting.get_secret_value().encode(),
                public_url=str(public_url_setting),
                now=now,
                connect=connect_mcp_server_with_token,
            )
        except McpServerReplaceRefusedError:
            # A server or token for this URL appeared after the pre-consume check.
            await self._refuse_replacement(interaction, consumed_row)
            return
        except (McpAgentGoneError, McpAttachFailedError) as err:
            # Exception class name only; SDK failures can include the request
            # envelope. Nothing was stored.
            _log.warning(
                "credential_modal.mcp_attach_failed",
                mcp_server_url=mcp_server_url,
                err_type=type(err.__cause__ or err).__name__,
            )
            await _refuse_for_unavailable_target(self._runtime, interaction, consumed_row)
            await interaction.followup.send(
                f"This request was used, but `{mcp_server_url}` could not be attached to the "
                "agent. Nothing was saved. Ask for a new request to retry.",
                ephemeral=True,
            )
            return
        except McpTokenWriteFailedError as err:
            _log.warning(
                "credential_modal.mcp_write_failed",
                mcp_server_url=mcp_server_url,
                err_type=type(err.__cause__ or err).__name__,
            )
            is_queued = await _settle_spent_request(
                self._runtime,
                row=consumed_row,
                outcome="write_failed",
                carries_work=False,
            )
            await edit_posted_card(
                interaction.client,
                row=consumed_row,
                state="partial",
                outcome=ConfigurationChange(
                    target_name=_agent_name(consumed_row),
                    kind="mcp",
                    availability="preparation_failed",
                    detail=consumed_row.target,
                ),
            )
            await interaction.followup.send(
                f"`{mcp_server_url}` is attached, but saving its token did not finish. "
                "Ask for a new request to retry.",
                ephemeral=True,
            )
            if is_queued:
                await _dispatch_origin_thread(interaction, consumed_row)
            return

        is_continuation_queued = await _settle_spent_request(
            self._runtime, row=consumed_row, outcome="applied", carries_work=True
        )
        await edit_posted_card(
            interaction.client,
            row=consumed_row,
            state="applied",
            outcome=ConfigurationChange(
                target_name=_agent_name(consumed_row),
                kind="mcp",
                availability="next_message",
                detail=consumed_row.target,
            ),
        )
        if is_continuation_queued:
            await _dispatch_origin_thread(interaction, consumed_row)


class SkillRepoModal(discord.ui.Modal):
    """Collect a GitHub token, import skills, and attach them to the requested agent.

    This requester-only enrollment verifies the token against the skill repo
    and stores it as that repo's SKILL credential
    (`set_skill_repo_credential`), which is the row later skill syncs resolve
    the token from. It deliberately writes no `agent_repo_binding`: the
    working repo is what the agent clones and runs, a separate decision with
    its own admin gate, and a skill import must never move it. The attach is
    gated like the repo bind (`skill_repo_connect`): a member is refused on a
    shared agent before the consume, and nobody attaches to a defaults-managed
    agent.
    """

    def __init__(self, *, runtime: DiscordRuntime, request_row: CredentialRequestRow) -> None:
        agent = _agent_name(request_row)
        url, branch, _path = split_skill_repo_target(request_row.target)
        owner_repo = normalize_owner_repo(url)
        super().__init__(title=_title(owner_repo, agent, "Your GitHub token"))
        self._runtime = runtime
        self._row = request_row
        self.add_item(
            discord.ui.TextDisplay(
                f"-# For **{owner_repo}**, branch `{branch}`. Skill repo only — the working "
                f"repo does not change. The token stays yours; {agent} uses it whenever it "
                "needs GitHub."
            )
        )
        token_label: discord.ui.Label[SkillRepoModal] = discord.ui.Label(
            text="Token",
            description="a fine-grained token with read access to the repo",
            component=discord.ui.TextInput(
                placeholder="github_pat_…",
                required=True,
                # Discord rejects the whole form with 50035 above 4000, so this
                # is a UI character cap and NOT MAX_SECRET_VALUE_BYTES (4096),
                # which is a byte cap enforced on submit. EnvCredentialModal
                # keeps the two separate for the same reason.
                max_length=4000,
            ),
        )
        self.pat_in = _text_input_of(token_label)
        self.add_item(token_label)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        if not is_credential_interaction_valid(interaction, self._row):
            await interaction.followup.send(_NO_LONGER_VALID, ephemeral=True)
            return

        if await refuse_if_shared_and_not_admin_for_request(
            interaction,
            runtime=self._runtime,
            tenant_id=self._row.tenant_id,
            agent_id=self._row.agent_id,
            operation="skill_repo_connect",
            caller_account_id=self._row.account_id,
        ):
            # Same shape as the repo bind: nothing is spent, and the card
            # stops offering a form this submitter could never finish.
            await _record_refused_outcome(self._runtime, self._row)
            await edit_posted_card(
                interaction.client, row=self._row, state="refused", refusal="admin_required"
            )
            return

        pat = str(self.pat_in.value or "").strip()
        if not pat:
            await interaction.followup.send("Enter a GitHub token.", ephemeral=True)
            return

        agent = await resolve_credential_target(interaction, runtime=self._runtime, row=self._row)
        if agent is None:
            return
        now = datetime.now(UTC)
        try:
            consumed_row = await consume_credential_submit(
                self._runtime.sessionmaker, row=self._row, agent=agent, now=now
            )
        except FormPinRefused as refused:
            await interaction.followup.send(refused.refusal, ephemeral=True)
            return
        if consumed_row is None:
            await interaction.followup.send(_NO_LONGER_VALID, ephemeral=True)
            return

        await edit_posted_card(interaction.client, row=consumed_row, state="received")

        url, branch, path = split_skill_repo_target(consumed_row.target)
        owner_repo = normalize_owner_repo(url)
        # The token appears only as a masked tail, never in full, and never
        # the (now-consumed) request token either.
        _log.info(
            "credential_modal.skill_repo.submit",
            repo_url=url,
            branch=branch,
            path=path,
            pat_present=bool(pat),
        )

        is_token_saved = False
        try:
            async with httpx.AsyncClient(timeout=30.0) as http_client:
                # Verify BEFORE storing: a token that cannot read this repo is
                # not a credential for it, and storing it would shadow a
                # working one on the next `get_pat` (the overlay is
                # last-write-wins, and tier 1 short-circuits the rest).
                if not await pat_can_access_repo(
                    http_client, owner_repo=normalize_owner_repo(url), pat=pat
                ):
                    await interaction.followup.send(
                        f"That token cannot read `{normalize_owner_repo(url)}`. Nothing was "
                        "stored, and the request was used up — ask again to retry.",
                        ephemeral=True,
                    )
                    return
                # Reuse the repo kind's resolver rather than calling
                # `store_inline_pat` directly: it returns the `ma_secret_ref`
                # AND the access proof that `set_binding` requires, so the two
                # writes cannot disagree about what was established.
                ma_secret_ref, proof = await resolve_repo_binding_credential(
                    self._runtime,
                    http_client,
                    agent_id=consumed_row.agent_id,
                    account_id=consumed_row.account_id,
                    repo_url=url,
                    pasted_pat=pat,
                    now=now,
                )
                is_token_saved = True
                # The skill repo's own credential row, keyed by (tenant,
                # agent, repo): a later sync of this repo resolves its token
                # from here. Without it the stored PAT is unreachable and
                # every sync falls back to an anonymous 404 that asks for the
                # credential again.
                async with self._runtime.sessionmaker.begin() as session:
                    seeded_skills = await write_skill_repo_submit(
                        session, row=consumed_row, ma_secret_ref=ma_secret_ref, proof=proof
                    )
                outcomes = await run_skill_sync(
                    self._runtime.anthropic,
                    http_client,
                    url=url,
                    branch=branch,
                    path=path,
                    tenant_id=consumed_row.tenant_id,
                    seeded_skills=seeded_skills,
                    is_admin=is_guild_admin(interaction),  # pyright: ignore[reportArgumentType]  # see refuse_if_shared_and_not_admin_for_request
                    token=pat,
                )
        except DaimonError as err:
            # Validation can fail before storage; preserve only confirmed saves.
            progress = (
                "Token saved, but the skill import did not finish."
                if is_token_saved
                else "Could not finish saving the token and importing skills. "
                "Some changes may have been saved."
            )
            await interaction.followup.send(
                f"{progress} {err} This request was used; ask again to retry the import.",
                ephemeral=True,
            )
            if is_token_saved:
                await self._render_import_failed(interaction, consumed_row, repo=owner_repo)
            return
        except Exception as err:
            _log.exception(
                "credential_modal.skill_repo_sync_failed",
                repo_url=url,
                err_type=type(err).__name__,
            )
            progress = (
                "Token saved, but the skill import did not finish."
                if is_token_saved
                else "Could not finish saving the token and importing skills. "
                "Some changes may have been saved."
            )
            await interaction.followup.send(
                f"{progress} Ask again to retry the import for `{owner_repo}`.",
                ephemeral=True,
            )
            if is_token_saved:
                await self._render_import_failed(interaction, consumed_row, repo=owner_repo)
            return

        imported = [o for o in outcomes if in_library(o)]
        failure_detail = summarize_failed_imports(outcomes)
        if not imported:
            # Nothing reached the library (an empty repo, or every skill
            # refused or failed), which the `applied` card must not claim.
            await self._render_import_failed(
                interaction, consumed_row, repo=owner_repo, detail=failure_detail
            )
            return

        attach_failure = await self._attach_to_requested_agent(
            tenant_id=consumed_row.tenant_id,
            agent_id=consumed_row.agent_id,
            outcomes=imported,
        )
        if attach_failure is not None:
            # The skills are in the library but not on the agent: the waiting
            # task gets nothing to resume with, and the card says so.
            await interaction.followup.send(
                f"Skills imported to the library, but not added to {_agent_name(consumed_row)}. "
                f"{attach_failure}",
                ephemeral=True,
            )
            is_queued = await _settle_spent_request(
                self._runtime, row=consumed_row, outcome="write_failed", carries_work=False
            )
            await edit_posted_card(
                interaction.client,
                row=consumed_row,
                state="partial",
                outcome=ConfigurationChange(
                    target_name=_agent_name(consumed_row),
                    kind="skills_bulk",
                    availability="saved",
                    count=len(imported),
                    repo=owner_repo,
                    detail="\n".join(line for line in (attach_failure, failure_detail) if line),
                ),
            )
            if is_queued:
                await _dispatch_origin_thread(interaction, consumed_row)
            return
        is_continuation_queued = await _settle_spent_request(
            self._runtime, row=consumed_row, outcome="applied", carries_work=True
        )
        await edit_posted_card(
            interaction.client,
            row=consumed_row,
            state="applied",
            outcome=ConfigurationChange(
                target_name=_agent_name(consumed_row),
                kind="skills_bulk",
                availability="next_message",
                count=len(imported),
                repo=owner_repo,
                detail=failure_detail,
            ),
        )
        if is_continuation_queued:
            await _dispatch_origin_thread(interaction, consumed_row)

    async def _render_import_failed(
        self,
        interaction: discord.Interaction,
        row: CredentialRequestRow,
        *,
        repo: str,
        detail: str | None = None,
    ) -> None:
        """Card and trail for a stored token whose skills did not import.

        The continuation carries no work: the skills the waiting task needs
        are not there, so no turn should resume as though they were.
        """
        is_queued = await _settle_spent_request(
            self._runtime, row=row, outcome="write_failed", carries_work=False
        )
        await edit_posted_card(
            interaction.client,
            row=row,
            state="partial",
            outcome=ConfigurationChange(
                target_name=_agent_name(row),
                kind="skills_bulk",
                availability="preparation_failed",
                # The failed line names no count; `skills_bulk` still
                # requires one, and nothing was imported to count.
                count=1,
                repo=repo,
                detail=detail,
            ),
        )
        if is_queued:
            await _dispatch_origin_thread(interaction, row)

    async def _attach_to_requested_agent(
        self,
        *,
        tenant_id: uuid.UUID,
        agent_id: uuid.UUID,
        outcomes: list[ResourceOutcome],
    ) -> str | None:
        """Attach the just-imported skills to the agent this request named.

        Importing puts skills in the tenant's shared library; it does not put
        them on an agent. The request row already names the agent, so doing
        only the import leaves the user staring at an agent with no skills and
        no way to tell that anything worked.

        Returns None when there was nothing to attach or the attach landed,
        and a one-line reason for the failure otherwise — raising is
        not an option, because the import has already succeeded by the time
        this runs and both halves must be reported truthfully.
        """
        skill_ids = library_skill_ids(outcomes)
        if not skill_ids:
            return None
        agent = await find_agent_by_derived_uuid(
            self._runtime.anthropic, tenant_id=tenant_id, agent_id=agent_id
        )
        if agent is None:
            return "That agent no longer exists."
        if agent.metadata.get(MA_METADATA_KEY_MANAGED) == "true":
            # Admins included: an attach never stamps the reconciler's spec
            # hash, so the seeded agent would drift for good.
            return f"`{agent.name}` is a built-in agent. Fork it and add them to the fork."
        new_skills: list[BetaManagedAgentsSkillParams] = [
            {"type": "custom", "skill_id": skill_id} for skill_id in skill_ids
        ]

        async def _apply(fresh: BetaManagedAgentsAgent) -> BetaManagedAgentsAgent:
            merged = merge_skills_with_ma(new_skills, fresh)
            collision = await find_attach_mount_collision(
                self._runtime.anthropic, tenant_id=tenant_id, skills=merged
            )
            if collision is not None:
                raise DaimonError(f"cannot attach: {collision}")
            return await self._runtime.anthropic.beta.agents.update(
                fresh.id, version=fresh.version, skills=merged
            )

        try:
            await update_agent_with_version_retry(self._runtime.anthropic, agent.id, _apply)
        except (DaimonError, anthropic.APIStatusError) as err:
            _log.warning(
                "credential_modal.skill_repo_attach_failed",
                agent_id=str(agent_id),
                err_type=type(err).__name__,
            )
            return "Attaching them did not finish. Ask again to retry."
        return None


class RepoBindModal(discord.ui.Modal):
    """Give an agent access to a repo: one optional token, gate, atomic
    consume, then the shared credential resolution and the binding write.

    Neither the repo nor the branch is retyped here — both are fixed by the
    consumed row's `target`, which packs them exactly as the two skill-repo
    kinds do, the same way the env key and MCP server name are fixed for the
    sibling forms. The token stays optional because a public repo needs
    none. Unlike the siblings, submitting this one is additionally
    gated on `credential_repo_bind.refuse_if_shared_and_not_admin_for_request`
    (see the module docstring): a member who was an admin, or whose target
    was private, when the button was clicked may have lost either between
    click and submit, so the gate runs again here, immediately after
    `defer` and before the consume, rather than being trusted from the
    pre-filter alone.
    """

    def __init__(self, *, runtime: DiscordRuntime, request_row: CredentialRequestRow) -> None:
        agent = _agent_name(request_row)
        url, branch, _path = split_skill_repo_target(request_row.target)
        owner_repo = normalize_owner_repo(url)
        super().__init__(title=_title(owner_repo, agent, "Your GitHub token"))
        self._runtime = runtime
        self._row = request_row
        self.add_item(
            discord.ui.TextDisplay(
                f"-# For **{owner_repo}**, branch `{branch}`. The token stays yours; "
                f"{agent} uses it whenever it needs GitHub."
            )
        )
        token_label: discord.ui.Label[RepoBindModal] = discord.ui.Label(
            text="Token",
            description="leave this blank if the repo is public",
            component=discord.ui.TextInput(
                placeholder="github_pat_…",
                required=False,
                max_length=4000,
            ),
        )
        self.pat_in = _text_input_of(token_label)
        self.add_item(token_label)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        if not is_credential_interaction_valid(interaction, self._row):
            await interaction.followup.send(_NO_LONGER_VALID, ephemeral=True)
            return

        if await refuse_if_shared_and_not_admin_for_request(
            interaction,
            runtime=self._runtime,
            tenant_id=self._row.tenant_id,
            agent_id=self._row.agent_id,
            caller_account_id=self._row.account_id,
        ):
            # The gate runs before the consume, so the request is NOT spent —
            # an admin can still use this same card. The card itself is
            # terminal all the same: it tells the channel the bind was
            # refused and who can make it, and it carries no button back to a
            # form that would refuse again.
            await _record_refused_outcome(self._runtime, self._row)
            await edit_posted_card(
                interaction.client,
                row=self._row,
                state="refused",
                refusal="admin_required",
            )
            return

        pat = str(self.pat_in.value or "").strip()

        agent = await resolve_credential_target(interaction, runtime=self._runtime, row=self._row)
        if agent is None:
            return
        now = datetime.now(UTC)
        try:
            consumed_row = await consume_credential_submit(
                self._runtime.sessionmaker, row=self._row, agent=agent, now=now
            )
        except FormPinRefused as refused:
            await interaction.followup.send(refused.refusal, ephemeral=True)
            return
        if consumed_row is None:
            await interaction.followup.send(_NO_LONGER_VALID, ephemeral=True)
            return

        await edit_posted_card(interaction.client, row=consumed_row, state="received")

        # The row's target packs the branch the card promised; unpack it here
        # rather than asking for it again, so the binding cannot disagree with
        # what the person was shown.
        repo_url, branch, _path = split_skill_repo_target(consumed_row.target)

        # Log the repo and branch, and the token ONLY as a masked tail when
        # present — never the plain value, never the (now-consumed) request
        # token.
        _log.info(
            "credential_modal.repo.submit",
            repo_url=repo_url,
            branch=branch,
            pat_present=bool(pat),
        )

        try:
            async with httpx.AsyncClient() as http_client:
                ma_secret_ref, proof = await resolve_repo_binding_credential(
                    self._runtime,
                    http_client,
                    agent_id=consumed_row.agent_id,
                    account_id=consumed_row.account_id,
                    repo_url=repo_url,
                    pasted_pat=pat or None,
                    now=now,
                )
            async with self._runtime.sessionmaker.begin() as session:
                await write_repo_submit(
                    session,
                    row=consumed_row,
                    ma_secret_ref=ma_secret_ref,
                    proof=proof,
                    write=set_binding,
                )
        except DaimonError as err:
            # This copy is written for the user -- surface it verbatim, plus
            # the fact that the request itself was used up either way.
            await interaction.followup.send(
                f"{err} The request was used up — ask again to retry.",
                ephemeral=True,
            )
            return
        except Exception as err:
            _log.exception(
                "credential_modal.repo_write_failed",
                repo_url=repo_url,
                err_type=type(err).__name__,
            )
            # Keep exception details in the operator log; SDK failures can
            # include the request envelope.
            await interaction.followup.send(
                "This request was used, but connecting the working repo did not finish. "
                "Some changes may have been saved. Ask for a new request to retry.",
                ephemeral=True,
            )
            return

        is_continuation_queued = await _settle_spent_request(
            self._runtime, row=consumed_row, outcome="applied", carries_work=True
        )
        await edit_posted_card(
            interaction.client,
            row=consumed_row,
            state="applied",
            outcome=ConfigurationChange(
                target_name=_agent_name(consumed_row),
                kind="repo",
                availability="next_message",
                repo=normalize_owner_repo(repo_url),
                branch=branch,
                # Never `copy` or `leave`: this form binds a repo to an agent
                # that had none of this person's uncommitted work to carry, so
                # a card claiming either would be inventing one.
                unsaved_work=None,
            ),
        )
        if is_continuation_queued:
            await _dispatch_origin_thread(interaction, consumed_row)
