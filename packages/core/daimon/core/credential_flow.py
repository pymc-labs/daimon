"""What a credential form submission does once the platform has read the form.

The platform checks the clicker, reads the value and decides the role-based
gates; from the write onward core owns the order and the copy. The platform
supplies a `CredentialPort`: where a private reply goes, how the posted card
is edited and how the waiting thread resumes. Logs carry key names and
counts only, never a value.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol, TypedDict, Unpack

import structlog
from anthropic import AsyncAnthropic
from anthropic.types.beta import BetaManagedAgentsAgent
from cryptography.fernet import MultiFernet
from daimon.core.agent_pins import FormPinRefused
from daimon.core.continuity.messages import ChangeAvailability, ConfigurationChange
from daimon.core.credential_requests import (
    CredentialRequestOutcome,
    availability_for_request,
    mcp_permission_message,
)
from daimon.core.credential_submit import (
    EnvSubmitPlan,
    apply_env_file_submit,
    apply_env_submit,
    consume_credential_submit,
    guard_credential_save,
    settle_credential_submit,
    write_mcp_submit,
)
from daimon.core.env_file import EnvEntry, env_collision_line
from daimon.core.mcp_attach import McpConnectDecision, McpServerReplaceRefusedError
from daimon.core.mcp_oauth.discovery import McpTokenProbe
from daimon.core.mcp_token_check import check_token, rejected_token_message
from daimon.core.mcp_token_connect import (
    McpAgentGoneError,
    McpAttachFailedError,
    McpTokenWriteFailedError,
)
from daimon.core.platform_words import words_for
from daimon.core.posted_controls import (
    NO_LONGER_VALID_MESSAGE,
    CardState,
    RefusalReason,
    card_for_request,
    card_text,
)
from daimon.core.stores import credential_requests
from daimon.core.stores.agent_files import AgentEnvEncryptionRequiredError
from daimon.core.stores.domain import ChatPlatform, CredentialRequestRow
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

log = structlog.get_logger()

WRITE_FAILED_MESSAGE = "Something went wrong — please try again."
#: How many colliding keys a whole-file refusal names before it summarises the rest,
#: matching what `render_env_import_rejected` shows for a rejected file.
_COLLISIONS_SHOWN = 3


class CardEdit(TypedDict, total=False):
    """What a card edit may carry besides its state (`card_for_request`'s fields)."""

    outcome: ConfigurationChange | None
    refusal: RefusalReason | None
    refusal_lines: Sequence[str]
    replaces: str | None
    retry_reason: str | None
    saving_notice: str | None


class CredentialPort(Protocol):
    """One submission's platform: its private reply, its card and its resume."""

    @property
    def platform(self) -> ChatPlatform: ...

    @property
    def sessionmaker(self) -> async_sessionmaker[AsyncSession]: ...

    @property
    def log_prefix(self) -> str: ...

    def location_verified(self, row: CredentialRequestRow) -> bool:
        """True when this submission just proved it came from the place the form was posted.

        It decides the inside-only rule of a private channel's agent. A port that checks
        nothing says False: that proves no place, so the submitter counts as outside.
        """
        ...

    async def reply(self, text: str) -> None:
        """Tell only the submitter. A no-op where the form has closed with no reply path."""
        ...

    async def edit_card(
        self, row: CredentialRequestRow, state: CardState, **card: Unpack[CardEdit]
    ) -> None:
        """Edit the posted card in place. Never raises: the outcome is already durable."""
        ...

    async def resume(self, row: CredentialRequestRow) -> None:
        """Start the continuation the save queued in the request's origin thread."""
        ...

    def write_failed(
        self, kind: str, err: Exception, *, key: str | None = None, key_count: int | None = None
    ) -> None:
        """Record a failed write. The error may quote the value: never log its text."""
        ...


def collision_lines(collisions: Sequence[EnvEntry], held: frozenset[str]) -> tuple[str, ...]:
    """The keys a whole-file import would replace: names and line numbers, no values."""
    lines = [env_collision_line(entry, held) for entry in collisions[:_COLLISIONS_SHOWN]]
    if len(collisions) > _COLLISIONS_SHOWN:
        lines.append(f"…and {len(collisions) - _COLLISIONS_SHOWN} more.")
    return tuple(lines)


def _nothing_changed(platform: ChatPlatform, row: CredentialRequestRow) -> str:
    if platform == "discord":
        return "Nothing was changed. Take them out of the file, or ask to replace them by name."
    responder = row.responder_name or "Daimon"
    return f"Nothing was changed. Ask {responder} to replace a key you already have."


async def submit_env(
    port: CredentialPort,
    *,
    row: CredentialRequestRow,
    agent: BetaManagedAgentsAgent,
    value: str,
    plan: EnvSubmitPlan,
) -> None:
    """Consume, write and queue in one transaction, then walk the card to its end."""
    try:
        result = await apply_env_submit(
            port.sessionmaker,
            row=row,
            agent=agent,
            platform=port.platform,
            value=value,
            plan=plan,
            now=datetime.now(UTC),
            location_verified=port.location_verified(row),
        )
    except FormPinRefused as refused:
        # Decided with the consume: rolled back, nothing stored.
        return await port.reply(refused.refusal)
    except AgentEnvEncryptionRequiredError as err:
        # Rolled back with the consume: nothing stored, the request stays live.
        log.error(f"{port.log_prefix}.env_write_refused_no_crypto_keys")
        return await port.reply(str(err))
    except Exception as err:  # the submit's boundary: the person gets a reply
        port.write_failed("env", err, key=row.target)
        return await port.reply(WRITE_FAILED_MESSAGE)
    consumed, state = result.consumed, result.state
    if consumed is None:
        return await port.reply(NO_LONGER_VALID_MESSAGE)
    log.info(f"{port.log_prefix}.env.submit", key=consumed.target, state=state)
    # First: if the terminal edit fails, a card saying "Saving…" is a better
    # last state than one still offering a button that can only be refused.
    await port.edit_card(consumed, "received")
    if state != "applied":
        refusal: RefusalReason | None = None
        replaces = None
        if state == "refused":
            refusal, replaces = "replacement_admin_required", plan.shadowed
        await port.edit_card(consumed, state, refusal=refusal, replaces=replaces)
        card = card_for_request(consumed, state=state, refusal=refusal, replaces=replaces)
        return await port.reply(card_text(card))
    # The card is the receipt: no reply beside it.
    change = ConfigurationChange(
        target_name=consumed.target_name or "this agent",
        kind="key",
        detail=consumed.target,
        availability=availability_for_request(consumed),
    )
    await port.edit_card(consumed, "applied", outcome=change)
    if result.queued:
        await port.resume(consumed)


async def submit_env_file(
    port: CredentialPort,
    *,
    row: CredentialRequestRow,
    agent: BetaManagedAgentsAgent,
    entries: tuple[EnvEntry, ...],
) -> None:
    """Whole-file in both directions: a key already held, by name or alias, writes nothing."""
    try:
        consumed, collisions, queued, held = await apply_env_file_submit(
            port.sessionmaker,
            row=row,
            agent=agent,
            platform=port.platform,
            entries=entries,
            now=datetime.now(UTC),
            location_verified=port.location_verified(row),
        )
    except FormPinRefused as refused:
        return await port.reply(refused.refusal)
    except AgentEnvEncryptionRequiredError as err:
        log.error(f"{port.log_prefix}.env_file_refused_no_crypto_keys")
        return await port.reply(str(err))
    except Exception as err:  # the submit's boundary: the person gets a reply
        port.write_failed("env_file", err, key_count=len(entries))
        return await port.reply(WRITE_FAILED_MESSAGE)
    if consumed is None:
        return await port.reply(NO_LONGER_VALID_MESSAGE)
    # The save already finished, so the card goes straight to its final state.
    agent_name = consumed.target_name or "the agent"
    if collisions:
        lines = (*collision_lines(collisions, held), _nothing_changed(port.platform, consumed))
        log.info(
            f"{port.log_prefix}.env_file_collision",
            key_count=len(entries),
            collision_count=len(collisions),
        )
        await port.edit_card(consumed, "refused", refusal="env_file_invalid", refusal_lines=lines)
        return await port.reply("\n".join((f"No keys were saved for {agent_name}.", *lines)))
    log.info(
        f"{port.log_prefix}.env_file.submit",
        key_count=len(entries),
        keys=[entry.name for entry in entries],
    )
    change = ConfigurationChange(
        target_name=agent_name,
        kind="keys_bulk",
        availability=availability_for_request(consumed),
        count=len(entries),
    )
    await port.edit_card(consumed, "applied", outcome=change)
    if queued:
        await port.resume(consumed)


def spent_outcome(reason: RefusalReason) -> CredentialRequestOutcome:
    """The audit outcome of a spent request closed with `reason`."""
    if reason == "token_rejected":
        return "token_rejected"
    return "policy_refused" if reason == "mcp_permission_required" else "write_failed"


async def refuse_spent(
    port: CredentialPort, row: CredentialRequestRow, reason: RefusalReason
) -> None:
    """Close a spent request that wrote nothing; no work resumes on it.

    Leaving the card on `received` would have it say "Saving…" about a save
    that stopped, and nothing is known to have reached a store, so it ends
    `refused` rather than `partial`.
    """
    async with port.sessionmaker.begin() as session:
        await credential_requests.set_credential_request_outcome(
            session, token=row.token, outcome=spent_outcome(reason)
        )
    await port.edit_card(row, "refused", refusal=reason)


@dataclass(frozen=True)
class McpConnectConfig:
    """What a token connection needs from the deployment, read before the consume."""

    anthropic: AsyncAnthropic
    fernet: MultiFernet | None
    probe: McpTokenProbe | None
    jwt_secret: bytes
    public_url: str
    #: The connector; passed in so a platform module's name for it stays patchable.
    connect: Callable[..., Awaitable[None]]


async def submit_mcp(
    port: CredentialPort,
    *,
    row: CredentialRequestRow,
    agent: BetaManagedAgentsAgent,
    value: str,
    connect: McpConnectDecision,
    config: McpConnectConfig,
    now: datetime,
) -> None:
    """Consume, ask the server, attach, publish the token, then the vault copy.

    The replacement decision was taken against the live submitter before the
    consume. A refused token or attach spends the request and writes nothing;
    a stored-but-not-vaulted token is `partial` and promises no turn.
    """
    try:
        consumed = await consume_credential_submit(
            port.sessionmaker,
            row=row,
            agent=agent,
            now=now,
            location_verified=port.location_verified(row),
        )
    except FormPinRefused as refused:
        return await port.reply(refused.refusal)
    if consumed is None:
        return await port.reply(NO_LONGER_VALID_MESSAGE)
    async with guard_credential_save(
        port.sessionmaker,
        row=consumed,
        edit_retry=lambda retry, reason: port.edit_card(retry, "requested", retry_reason=reason),
        edit_pending=lambda pending, notice: port.edit_card(
            pending, "received", saving_notice=notice
        ),
    ) as attempt:
        await port.edit_card(consumed, "received")
        if connect.refused:
            attempt.retry_allowed = False
            return await _refuse_mcp(port, consumed, policy_refused=not connect.replaces)
        url = consumed.mcp_server_url
        if url is None:
            attempt.retry_allowed = False
            log.error(f"{port.log_prefix}.mcp_missing_server_url", agent_id=str(consumed.agent_id))
            await refuse_spent(port, consumed, "target_unavailable")
            return await port.reply("This request is missing its server URL — please ask again.")
        log.info(f"{port.log_prefix}.mcp.submit", mcp_server_url=url, token_present=bool(value))
        # Ask the server first. A token it rejects would otherwise be stored,
        # mirrored into every caller's vault and attached, and every turn from
        # then on would carry a failed MCP init. A server that cannot be
        # reached is not a verdict: the save proceeds and MA reports later.
        rejection = await check_token(config.probe, mcp_server_url=url, token=value)
        if rejection is not None:
            await refuse_spent(port, consumed, "token_rejected")
            return await port.reply(rejected_token_message(rejection))
        try:
            await write_mcp_submit(
                config.anthropic,
                session_factory=port.sessionmaker,
                row=consumed,
                fernet=config.fernet,
                value=value,
                replace_allowed=connect.replace_allowed,
                jwt_secret=config.jwt_secret,
                public_url=config.public_url,
                now=now,
                connect=config.connect,
            )
        except McpServerReplaceRefusedError:
            attempt.retry_allowed = False
            # A server or token for this URL appeared after the pre-consume check.
            return await _refuse_mcp(port, consumed)
        except (McpAgentGoneError, McpAttachFailedError) as err:
            attempt.retry_allowed = not isinstance(err, McpAgentGoneError)
            # Class name only: an SDK failure can carry the request envelope.
            log.warning(
                f"{port.log_prefix}.mcp_attach_failed",
                mcp_server_url=url,
                err_type=type(err.__cause__ or err).__name__,
            )
            await refuse_spent(port, consumed, "target_unavailable")
            return await port.reply(
                f"`{url}` could not be attached to the agent. Nothing was saved. "
                "Try again using the same form."
            )
        except McpTokenWriteFailedError as err:
            log.warning(
                f"{port.log_prefix}.mcp_write_failed",
                mcp_server_url=url,
                err_type=type(err.__cause__ or err).__name__,
            )
            await settle_credential_submit(
                port.sessionmaker,
                row=consumed,
                platform=port.platform,
                outcome="write_failed",
                carries_work=False,
            )
            change = _mcp_change(consumed, availability="preparation_failed")
            await port.edit_card(consumed, "partial", outcome=change)
            verb = "saving" if port.platform == "discord" else "storing"
            return await port.reply(
                f"`{url}` is attached, but {verb} its token did not finish. "
                "Try again using the same form."
            )
        queued = await settle_credential_submit(
            port.sessionmaker, row=consumed, platform=port.platform, outcome="applied"
        )
        # The card is the receipt: no reply beside it.
        await port.edit_card(
            consumed, "applied", outcome=_mcp_change(consumed, availability="next_message")
        )
        if queued:
            await port.resume(consumed)


def _mcp_change(
    row: CredentialRequestRow, *, availability: ChangeAvailability
) -> ConfigurationChange:
    return ConfigurationChange(
        target_name=row.target_name or "the agent",
        kind="mcp",
        detail=row.target,
        availability=availability,
    )


async def _refuse_mcp(
    port: CredentialPort, row: CredentialRequestRow, *, policy_refused: bool = False
) -> None:
    """Spend the request and write nothing: no vault token, no published token."""
    await settle_credential_submit(
        port.sessionmaker,
        row=row,
        platform=port.platform,
        outcome="policy_refused" if policy_refused else "write_failed",
        carries_work=False,
    )
    agent_name = row.target_name or "The agent"
    if policy_refused:
        await port.edit_card(row, "refused", refusal="mcp_permission_required")
        return await port.reply(mcp_permission_message(agent_name) + " Nothing was saved.")
    await port.edit_card(row, "refused", refusal="replacement_admin_required")
    admin = words_for(port.platform).admin
    await port.reply(
        f"{agent_name} already has this connection. Nothing was saved."
        f"\n\nOnly {admin} can replace it."
    )
