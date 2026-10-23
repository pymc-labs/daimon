"""Discord rendering over the shared, authenticated fleet operations.

The host supplies its current-root loader. A card handle never supplies root
authority, and an operation acknowledgement never certifies native completion.
"""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from collections.abc import Awaitable, Callable, Iterator, MutableMapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Self, cast

import structlog
from daimon.adapters.discord.checks import ADMIN_NOUN
from daimon.adapters.discord.posted_controls.view import build_card_view
from daimon.adapters.discord.views import finish_control
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.defaults.metadata import MA_METADATA_KEY_NAME
from daimon.core.fleet import TERMINAL, FleetRun, FleetWorker, holds_slot
from daimon.core.fleet_orchestration import Control, FleetOrchestrator, FleetRefused
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.neutral_resources import WorkerPermissions, WorkerSpecRefused
from daimon.core.posted_controls import CardButton, CardState, PostedCard
from daimon.core.stores.agent_posts import AgentPostRow
from daimon.core.tool_safety import DAIMON_SERVER_NAME
from daimon.core.turn.state import ToolUseBlock, TurnState

import discord

if TYPE_CHECKING:
    from daimon.adapters.discord.bot import DaimonBot
    from daimon.core.turn.deps import TurnDeps

type ParentLoader = Callable[
    [discord.Interaction | None], Awaitable[tuple[WorkerPermissions, TenantAccessPolicy] | None]
]
type ParentResolver = Callable[
    [discord.Thread, int, uuid.UUID | None, uuid.UUID | None, discord.Interaction | None],
    Awaitable[tuple[WorkerPermissions, TenantAccessPolicy] | None],
]
type Send = Callable[..., Awaitable[discord.Message]]
type Edit = Callable[..., Awaitable[discord.Message | None]]
type FindPosted = Callable[[uuid.UUID], Awaitable[discord.Message | None]]

_MESSAGE_TEXT_LIMIT = 4000
# Cards remembered per run so a wake turn edits the card its root turn posted.
_POSTED_CARDS_LIMIT = 512
# Recent thread messages searched for a run's card the process no longer remembers.
_CARD_HISTORY_LIMIT = 50
_UNAVAILABLE = "Fleet status is unavailable, so there's no fleet result to share."
_FINISHED = "The fleet workers have finished. I'll post the result here next."
_ERROR_CODE = re.compile(r"[a-z0-9_]{1,40}")

log = structlog.get_logger(__name__)


def _literal(value: str, limit: int) -> str:
    return (
        discord.utils.escape_markdown(value[:limit])
        .replace("@", "@\u200b")
        .replace("<#", "<\u200b#")
    )[:limit]


def build_fleet_view(
    run: FleetRun,
    workers: Sequence[FleetWorker],
    *,
    collected: bool = False,
    blocked: bool = False,
    partial: bool = False,
    without: Sequence[str] = (),
) -> discord.ui.LayoutView:
    """Render only canonical scoped rows; no specs, excerpts or native handles.

    `without` names every login the workers run without, with the reason (frozen
    with the root), so a login is never dropped silently.
    """
    if (
        run.platform != "discord"
        or len({worker.id for worker in workers}) != len(workers)
        or len(workers) > 2 * run.max_workers
        or sum(1 for worker in workers if holds_slot(worker)) > run.max_workers
        or any(worker.run_id != run.id or worker.tenant_id != run.tenant_id for worker in workers)
    ):
        raise FleetRefused("foreign_worker")
    state: CardState = (
        "applied"
        if collected
        else "refused"
        if partial or blocked or run.status in ("cancelled", "failed")
        else "received"
    )
    facts = tuple(
        f"{worker.ordinal}. **{_literal(worker.role, 60)}**{_worker_agent(worker)} · "
        f"{_literal(worker.backend, 20)} / {_literal(worker.model, 40)} — "
        f"{_worker_status(worker)}"
        for worker in sorted(workers, key=lambda row: row.ordinal)
    )
    if without:
        facts += ("Workers run without: " + _literal("; ".join(without), 600),)
    actions: tuple[tuple[Control, str], ...] = (("kill", "Stop"), ("resume", "Resume"))

    def controls(target: str, version: int, label: str) -> tuple[CardButton, ...]:
        result: list[CardButton] = []
        for action, caption in actions:
            custom_id = f"fleet:{run.id.hex}:{target}:{version}:{action}"
            if len(custom_id) > 100:
                raise FleetRefused("invalid_control")
            result.append(
                CardButton(label=f"{caption} {label}", style="primary", custom_id=custom_id)
            )
        return tuple(result)

    buttons = (
        controls("all", run.version, "all")
        if (not collected and run.status in ("running", "collecting"))
        else ()
    )
    card = PostedCard(
        kind="fleet",
        state=state,
        headline=(
            "Fleet — collected"
            if collected
            else "Fleet — partial result"
            if partial
            else "Fleet — blocked"
            if blocked
            else "Fleet — no workers started"
            if not workers and run.status in ("completed", "cancelled", "failed")
            else f"Fleet — {run.status}"
        ),
        facts=facts,
        buttons=buttons,
        footer=(
            f"Budget cap ${run.total_budget_usd} · "
            f"Deadline <t:{int(run.deadline_at.timestamp())}:f>"
        ),
    )
    view = build_card_view(card, button_factory=FleetControl.from_button)
    # Newer active workers receive individual controls
    # first; the run-level pair always remains available for the rest.
    individually_controlled: set[uuid.UUID] = set()
    if buttons:
        for worker in sorted(workers, key=lambda row: row.ordinal, reverse=True):
            if worker.status in TERMINAL:
                continue
            candidate_buttons = buttons + controls(
                worker.id.hex, worker.version, str(worker.ordinal)
            )
            try:
                candidate = build_card_view(
                    card.model_copy(update={"buttons": candidate_buttons}),
                    button_factory=FleetControl.from_button,
                )
            except ValueError as error:
                # The base card and typed control IDs are valid. Only the SDK's
                # layout capacity may stop adding this pair; owner refusals escape.
                if type(error) is not ValueError:
                    raise
                break
            view.stop()
            view, buttons = candidate, candidate_buttons
            individually_controlled.add(worker.id)
    if buttons and any(
        worker.status not in TERMINAL and worker.id not in individually_controlled
        for worker in workers
    ):
        view.stop()
        facts = tuple(
            fact
            + (
                " · status only"
                if worker.status not in TERMINAL and worker.id not in individually_controlled
                else ""
            )
            for worker, fact in zip(
                sorted(workers, key=lambda row: row.ordinal), facts, strict=True
            )
        )
        view = build_card_view(
            card.model_copy(update={"buttons": buttons, "facts": facts}),
            button_factory=FleetControl.from_button,
        )
    card = card.model_copy(update={"buttons": buttons, "facts": facts})
    retained = len(facts)
    # Count the actual SDK text, including Markdown, status labels and the footer.
    # Keep complete rows in canonical order and count every omitted row.
    while retained and view.content_length() > _MESSAGE_TEXT_LIMIT:
        retained -= 1
        view.stop()
        view = build_card_view(
            card.model_copy(
                update={"facts": facts[:retained] + (f"+{len(facts) - retained} more",)}
            ),
            button_factory=FleetControl.from_button,
        )
    if view.content_length() > _MESSAGE_TEXT_LIMIT:
        view.stop()
        raise FleetRefused("invalid_card")
    return view


def _worker_agent(worker: FleetWorker) -> str:
    """The agent a worker ran as, when it ran as one and that isn't its role's name.

    A worker that runs as an agent carries that agent's own metadata name. An inline
    spec inherits the root's metadata, so a name it gives itself is not shown as an
    agent.
    """
    name, metadata = worker.spec.get("name"), worker.spec.get("metadata")
    agent = metadata.get(MA_METADATA_KEY_NAME) if isinstance(metadata, dict) else None
    drops = worker.spec.get("declared_drops")
    declared = [
        item for item in (drops if isinstance(drops, list) else []) if isinstance(item, str)
    ]
    if not isinstance(name, str) or not name.strip() or name != agent:
        # A task copy of the root: every one runs without tools and skills, so say only
        # what the root's own setup lost (its connectors, stored files, repositories).
        lost = [_DROP_WORDS[item] for item in declared if item in _COPY_LOSSES]
        return f" (without {', '.join(dict.fromkeys(lost))})" if lost else ""
    # What it runs without, so the person who asked sees it, not only the root.
    lost = [_DROP_WORDS.get(item, "some of its setup") for item in declared]
    without = f", without {', '.join(dict.fromkeys(lost))}" if lost else ""
    label = "" if name == worker.role else _literal(name, 40)
    if not label and not without:
        return ""
    return f" ({label}{without})" if label else f" ({without.removeprefix(', ')})"


#: Losses a task copy of the root shows; the rest every task worker has.
_COPY_LOSSES = frozenset({"mcp", "files", "repository"})

#: Plain words for what an agent worker runs without (its declared losses).
_DROP_WORDS = {
    "tools": "its tools",
    "skills": "its skills",
    "mcp": "its connected tools",
    "memory": "its memory",
    "sandbox": "its sandbox",
    "model_controls": "its model settings",
    "files": "its files",
    "repository": "its repository",
    "credentials": "its saved keys",
}


def _worker_status(worker: FleetWorker) -> str:
    """Status words, with the closed error code of a worker that did not succeed."""
    status = worker.status.replace("_", " ")
    if worker.status in TERMINAL and worker.status != "succeeded" and worker.error_code:
        from daimon.core.fleet import worker_error_text

        # Known codes in plain words; others stay closed host codes in code spans.
        if (text := worker_error_text(worker.error_code)) is not None:
            return f"{status}: {text}"
        code = worker.error_code if _ERROR_CODE.fullmatch(worker.error_code) else "error"
        status += f" (`{code}`)"
    return status


def _worker_list(workers: Sequence[FleetWorker], *, limit: int = 1400) -> str:
    """Complete role/status pairs within one Discord message, in canonical order."""
    facts = [
        f"{_literal(row.role, 60)}: {_worker_status(row)}"
        for row in sorted(workers, key=lambda row: row.ordinal)
    ]
    retained: list[str] = []
    for fact in facts:
        if len("; ".join(retained + [fact])) > limit:
            break
        retained.append(fact)
    if len(retained) < len(facts):
        retained.append(f"+{len(facts) - len(retained)} more")
    return "; ".join(retained)


def _collect_status(block: ToolUseBlock) -> str | None:
    """The status a completed daimon fleet_collect call returned, or None."""
    for item in block.result_content or ():
        text: object = (
            cast("dict[str, object]", item).get("text")
            if isinstance(item, dict)
            else getattr(item, "text", None)
        )
        if not isinstance(text, str):
            continue
        try:
            value: object = json.loads(text)
        except ValueError:
            continue
        for _ in range(3):
            if not isinstance(value, dict):
                break
            reply = cast("dict[str, object]", value)
            if isinstance(reply.get("status"), str) and "run_id" in reply:
                return cast(str, reply["status"])
            value = reply.get("result", reply.get("structuredContent"))
    return None


def fleet_result_seen(state: TurnState) -> bool:
    """Whether the root's final answer follows a collect that returned the run's end.

    Only the turn's last fleet_collect counts: a waiting, failed or absent one
    means the answer predates any finished result, so a wake must deliver it.
    """
    for block in reversed(state.content):
        if not isinstance(block, ToolUseBlock) or block.mcp_server_name != DAIMON_SERVER_NAME:
            continue
        if block.name != "fleet_collect" and not (
            block.name == "call_tool" and block.input.get("name") == "fleet_collect"
        ):
            continue
        if block.status != "complete" or block.is_error:
            return False
        return _collect_status(block) in ("ready", "blocked")
    return False


def _custom_ids(components: Sequence[object]) -> Iterator[str]:
    for component in components:
        custom_id = getattr(component, "custom_id", None)
        if isinstance(custom_id, str):
            yield custom_id
        for name in ("children", "components"):
            nested = getattr(component, name, None)
            if isinstance(nested, (list, tuple)):
                yield from _custom_ids(cast("Sequence[object]", nested))
        accessory = getattr(component, "accessory", None)
        if accessory is not None:
            yield from _custom_ids((accessory,))


def history_finder(thread: discord.Thread) -> FindPosted:
    """Find a run's live card in recent thread history (its controls name the run)."""

    async def find(run_id: uuid.UUID) -> discord.Message | None:
        prefix = f"fleet:{run_id.hex}:"
        try:
            async for message in thread.history(limit=_CARD_HISTORY_LIMIT):
                if (message.webhook_id is not None or message.author.bot) and any(
                    custom_id.startswith(prefix) for custom_id in _custom_ids(message.components)
                ):
                    return message
        except Exception:
            # A lookup is best effort: failing it posts a fresh card instead.
            log.info("fleet.card_history_unavailable", run_id=str(run_id), exc_info=True)
        return None

    return find


class FleetControl(
    discord.ui.DynamicItem[discord.ui.Button[discord.ui.LayoutView]],
    template=r"fleet:(?P<run>[0-9a-f]{32}):(?P<worker>all|[0-9a-f]{32}):(?P<version>[0-9]+):(?P<action>kill|resume)",
):
    """The same durable, host-reconstructed control for live and restarted clients."""

    def __init__(self, button: discord.ui.Button[discord.ui.LayoutView]) -> None:
        super().__init__(button)
        match = self.template.fullmatch(button.custom_id or "")
        if match is None:
            raise FleetRefused("invalid_control")
        self.run_id = uuid.UUID(hex=match["run"])
        self.worker_id = None if match["worker"] == "all" else uuid.UUID(hex=match["worker"])
        self.expected_version = int(match["version"])
        self.action: Control = cast(Control, match["action"])

    @classmethod
    def from_button(cls, button: CardButton) -> Self:
        return cls(
            discord.ui.Button(
                style=discord.ButtonStyle.primary, label=button.label, custom_id=button.custom_id
            )
        )

    @classmethod
    async def from_custom_id(
        cls,
        interaction: discord.Interaction,
        item: discord.ui.Item[Any],
        match: re.Match[str],
        /,
    ) -> Self:
        return cls(
            discord.ui.Button(
                style=discord.ButtonStyle.primary,
                label=getattr(item, "label", None),
                custom_id=match[0],
            )
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        bot = cast("DaimonBot", interaction.client)
        channel = interaction.channel
        if not isinstance(channel, discord.abc.Messageable) or interaction.message is None:
            await interaction.response.send_message(
                "This fleet control is unavailable.", ephemeral=True
            )
            return

        card = await bot.recover_fleet_card(interaction, run_id=self.run_id)
        if card is None or card.run_id != self.run_id:
            await interaction.response.send_message(
                "This fleet control is unavailable.", ephemeral=True
            )
            return
        await card.control(
            interaction,
            worker_id=self.worker_id,
            action=self.action,
            expected_version=self.expected_version,
        )


class DiscordFleetCardFactory:
    """Bind the installed shared owner to an actual Discord root and post path.

    The resolver proves the exact turn intent/origin and fresh current Subject.
    Only proven absence may return None; missing or foreign authority raises.
    No database, provider or permission projection is invented by this factory.
    """

    def __init__(self, service: FleetOrchestrator, load_parent: ParentResolver) -> None:
        self._service = service
        self._resolve = load_parent
        # One live card per run in this process: a wake turn edits the card its
        # root turn posted instead of leaving that one at "running".
        self._posted: dict[uuid.UUID, discord.Message] = {}

    @classmethod
    def from_runtime(cls, service: FleetOrchestrator, deps: TurnDeps) -> Self:
        """Bind the real intent/root owner, retaining its original scope."""
        from daimon.core.mux_backend import resource_scope
        from daimon.core.native_admins import load_live_subject, load_stored_subject
        from daimon.core.stores.identity import find_platform_principal
        from daimon.core.stores.mux_state import get_fleet_root_operation
        from daimon.core.stores.turn_card_intents import get_turn_card_intent
        from daimon.core.turn.runtime_manifest import read_frozen_root
        from mux.errors import ScopeViolation
        from sqlalchemy.exc import SQLAlchemyError

        async def resolve(
            thread: discord.Thread,
            requester_id: int,
            intent_id: uuid.UUID | None,
            run_id: uuid.UUID | None,
            interaction: discord.Interaction | None,
        ) -> tuple[WorkerPermissions, TenantAccessPolicy] | None:
            from daimon.adapters.discord.checks import admin_caller

            if intent_id is None or (interaction is not None and run_id is None):
                raise FleetRefused("prerequisites_unavailable")
            tenant_id = derive_tenant_uuid(platform="discord", workspace_id=str(thread.guild.id))
            try:
                async with deps.sessionmaker() as db:
                    original = await find_platform_principal(
                        db, tenant_id=tenant_id, platform="discord", external_id=str(requester_id)
                    )
                    if original is None:
                        raise FleetRefused("prerequisites_unavailable")
                    subject = (
                        await load_stored_subject(
                            db,
                            tenant_id=tenant_id,
                            platform="discord",
                            account_id=original.account_id,
                            platform_user_id=str(requester_id),
                        )
                        if interaction is None
                        else await load_live_subject(
                            db,
                            tenant_id=tenant_id,
                            platform="discord",
                            caller=admin_caller(interaction.user),
                        )
                    )
                scope = resource_scope(
                    tenant_id=str(tenant_id),
                    account_id=str(original.account_id),
                    authorization_id="discord-fleet-card",
                )
                if run_id is None:
                    # This thread's own turn that never froze a fleet root has no
                    # fleet card. The durable intent and root records prove that
                    # absence; the render task has no live persistence context.
                    # Unknown or foreign intents still go through the refusing path.
                    async with deps.sessionmaker() as db:
                        own = await get_turn_card_intent(db, id=intent_id)
                        frozen = await get_fleet_root_operation(
                            db, scope, intent_id=intent_id, check_intent_owner=True
                        )
                    if (
                        own is not None
                        and own.tenant_id == tenant_id
                        and own.platform == "discord"
                        and own.thread_id == str(thread.id)
                        and frozen is None
                    ):
                        return None
                result = await read_frozen_root(
                    deps,
                    scope,
                    intent_id=intent_id,
                    run_id=run_id,
                    now=datetime.now(UTC),
                    drain=True,
                    allow_absent=True,
                )
                if result is None:
                    return None
                parent, policy = result
                if parent.subject.platform_user_id != str(requester_id):
                    raise FleetRefused("foreign_run")
                return replace(parent, subject=subject), policy
            except (WorkerSpecRefused, ScopeViolation, SQLAlchemyError):
                raise FleetRefused("prerequisites_unavailable") from None

        return cls(service, resolve)

    def __call__(
        self,
        thread: discord.abc.Messageable,
        requester_id: int,
        send: Send,
        edit: Edit,
        *,
        run_id: uuid.UUID | None = None,
        turn_card_intent_id: uuid.UUID | None = None,
        recorded_post: AgentPostRow | None = None,
        on_replacement: Callable[[discord.Message], Awaitable[None]] | None = None,
        wake_id: uuid.UUID | None = None,
    ) -> DiscordFleetCard | None:
        if not isinstance(thread, discord.Thread):
            return None
        card: DiscordFleetCard

        async def current(interaction: discord.Interaction | None):
            actor_id = interaction.user.id if interaction is not None else requester_id
            result = await self._resolve(
                thread, requester_id, turn_card_intent_id, card.run_id, interaction
            )
            if result is None:
                if card.run_id is not None:
                    raise FleetRefused("foreign_run")
                return None
            parent, policy = result
            root = parent.fleet.root
            if (
                root.channel.platform != "discord"
                or root.thread_id != str(thread.id)
                or root.channel.channel_id != str(thread.parent_id)
                or parent.fleet.scope.tenant_id
                != str(derive_tenant_uuid(platform="discord", workspace_id=str(thread.guild.id)))
                or parent.subject.platform_user_id != str(actor_id)
            ):
                raise FleetRefused("foreign_run")
            if recorded_post is not None and (
                parent.agent_id != recorded_post.agent_id
                or str(requester_id) != recorded_post.requester_platform_user_id
                or recorded_post.tenant_id != uuid.UUID(parent.fleet.scope.tenant_id)
            ):
                raise FleetRefused("foreign_run")
            return parent, policy

        card = DiscordFleetCard(
            run_id=run_id,
            service=self._service,
            load_parent=current,
            send=send,
            edit=edit,
            on_replacement=on_replacement,
            posted=self._posted,
            find_posted=history_finder(thread),
            wake_id=wake_id,
        )
        return card


_NOT_CONTROLLER = f"Only the person who started this fleet, or {ADMIN_NOUN}, can {{verb}} it."
_ALREADY_ENDED = "This fleet has already stopped."
_WORKER_ENDED = "This worker has already finished."
_UNREACHABLE = "Couldn't reach the fleet just now. Try again in a moment."
_NOT_HERE = "You can't control this fleet from here."
_LIMIT_USED = "This fleet has run out of the time or steps it was given, so it can't be resumed."
_IN_FLIGHT = "Someone's press on this fleet is still going through. Try again in a moment."
# Refusals a newer card would not offer: the card is behind the run.
_CARD_BEHIND = frozenset(
    {
        "stale_version",
        "foreign_worker",
        "foreign_run",
        "ambiguous_workers",
        # The run moved under the press (a concurrent control or wake).
        "control_evidence_invalid",
    }
)
# The same press, from this or another clicker, is still being written.
_PRESS_IN_FLIGHT = frozenset({"operation_conflict", "control_evidence_unavailable"})
_CHANGED = (
    "This fleet changed since the card was drawn. Wait for the card to update, then try again."
)


def _refusal_text(refused: FleetRefused | WorkerSpecRefused, action: Control) -> str:
    """Why a fleet card click did nothing, in terms of what the clicker can do."""
    reason: str = refused.reason
    if reason == "controller_required":
        # The shared owner's live rule: the fleet's starter or a current server admin.
        return _NOT_CONTROLLER.format(verb="stop" if action == "kill" else "resume")
    if reason == "terminal_run":
        return _ALREADY_ENDED
    if reason == "terminal_worker":
        return _WORKER_ENDED
    if reason in _CARD_BEHIND:
        return _CHANGED
    if reason == "original_limit_exhausted":
        return _LIMIT_USED
    if reason in _PRESS_IN_FLIGHT:
        return _IN_FLIGHT
    if reason == "prerequisites_unavailable":
        return _UNREACHABLE
    return _NOT_HERE


class DiscordFleetCard:
    """One orchestrator card; all reads and controls use the same core owner."""

    def __init__(
        self,
        *,
        run_id: uuid.UUID | None,
        service: FleetOrchestrator,
        load_parent: ParentLoader,
        send: Send,
        edit: Edit,
        on_replacement: Callable[[discord.Message], Awaitable[None]] | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        posted: MutableMapping[uuid.UUID, discord.Message] | None = None,
        find_posted: FindPosted | None = None,
        wake_id: uuid.UUID | None = None,
    ) -> None:
        self._run_id = run_id
        self._service = service
        self._load_parent = load_parent
        self._send = send
        self._edit = edit
        self._on_replacement = on_replacement
        self._clock = clock
        self._message: discord.Message | None = None
        self._view: discord.ui.LayoutView | None = None
        self._lock = asyncio.Lock()
        self._posted = posted
        self._find_posted = find_posted
        # The worker_finished wake this turn runs for: its own claim is not a
        # wake still owed to the root, and it settles through the wake owner.
        self._wake_id = wake_id
        # Logins the workers run without (frozen with the root), read with each snapshot.
        self._without: tuple[str, ...] = ()

    @property
    def run_id(self) -> uuid.UUID | None:
        """A lookup identity; the shared owner still proves root/run authority."""
        return self._run_id

    async def _snapshot(
        self, interaction: discord.Interaction | None
    ) -> tuple[WorkerPermissions, TenantAccessPolicy, FleetRun, tuple[FleetWorker, ...]] | None:
        result = await self._load_parent(interaction)
        if result is None:
            if self._run_id is not None:
                raise FleetRefused("foreign_run")
            return None
        parent, policy = result
        if interaction is not None and (
            parent.subject.platform_user_id != str(interaction.user.id)
            or interaction.guild_id is None
            or parent.fleet.scope.tenant_id
            != str(derive_tenant_uuid(platform="discord", workspace_id=str(interaction.guild_id)))
            or parent.fleet.root.channel.platform != "discord"
            or parent.fleet.root.thread_id != str(interaction.channel_id)
        ):
            raise FleetRefused("foreign_run")
        run_id = self._run_id or parent.fleet.run_id
        run, workers = await self._service.read_snapshot(parent, policy, run_id)
        self._run_id = run.id
        self._without = parent.workers_run_without
        return parent, policy, run, workers

    def _render(
        self,
        run: FleetRun,
        workers: tuple[FleetWorker, ...],
        *,
        collected: bool = False,
        blocked: bool = False,
        partial: bool = False,
    ) -> discord.ui.LayoutView:
        return build_fleet_view(
            run,
            workers,
            collected=collected,
            blocked=blocked,
            partial=partial,
            without=self._without,
        )

    async def refresh(self) -> bool:
        """Called from the existing render loop, never from a native event tap."""
        async with self._lock:
            snapshot = await self._snapshot(None)
            if snapshot is None:
                return False
            _, _, run, workers = snapshot
            view = self._render(run, workers)
            await self._display(view)
            return True

    async def _display(self, view: discord.ui.LayoutView) -> None:
        if self._message is None:
            if not await self._adopt(view):
                self._message = await self._send(
                    view=view, allowed_mentions=discord.AllowedMentions.none()
                )
        else:
            previous = self._message
            updated = await self._edit(
                previous, view=view, allowed_mentions=discord.AllowedMentions.none()
            )
            if updated is not None:
                self._message = updated
                if updated.id != previous.id and self._on_replacement is not None:
                    await self._on_replacement(updated)
        if self._view is not None:
            self._view.stop()
        self._view = view
        if self._posted is not None and self._run_id is not None and self._message is not None:
            self._posted.pop(self._run_id, None)
            self._posted[self._run_id] = self._message
            while len(self._posted) > _POSTED_CARDS_LIMIT:
                self._posted.pop(next(iter(self._posted)))

    async def _adopt(self, view: discord.ui.LayoutView) -> bool:
        """Edit the card an earlier turn of this run posted in this thread, if any."""
        if self._run_id is None:
            return False
        previous = self._posted.get(self._run_id) if self._posted is not None else None
        if previous is None and self._find_posted is not None:
            # A restart forgot the card; its run-scoped controls still find it.
            previous = await self._find_posted(self._run_id)
        if previous is None:
            return False
        try:
            updated = await self._edit(
                previous, view=view, allowed_mentions=discord.AllowedMentions.none()
            )
        except discord.HTTPException:
            # The earlier card is gone or no longer ours: post this turn's own.
            log.info("fleet.card_adopt_failed", run_id=str(self._run_id))
            return False
        self._message = updated if updated is not None else previous
        if updated is not None and updated.id != previous.id and self._on_replacement is not None:
            await self._on_replacement(updated)
        return True

    async def control(
        self,
        interaction: discord.Interaction,
        *,
        worker_id: uuid.UUID | None,
        action: Control,
        expected_version: int,
    ) -> bool:
        async with self._lock:
            try:
                snapshot = await self._snapshot(interaction)
                if snapshot is None:
                    raise FleetRefused("foreign_run")
                parent, policy, run, _ = snapshot
                reply = await self._service.control(
                    parent,
                    policy,
                    run_id=run.id,
                    worker_id=worker_id,
                    action=action,
                    expected_version=expected_version,
                    operation_key=(
                        f"discord:fleet:{self._run_id}:{worker_id or 'all'}:"
                        f"{expected_version}:{action}"
                    ),
                    now=self._clock(),
                )
                if reply.get("accepted") is not True:
                    await interaction.response.send_message(
                        "This action is waiting for its dependencies.", ephemeral=True
                    )
                    return False
                log.info(
                    "fleet.control_pressed",
                    run_id=str(run.id),
                    worker_id=str(worker_id) if worker_id is not None else None,
                    action=action,
                    pressed_by=str(interaction.user.id),
                    presser_is_admin=parent.subject.is_admin,
                )
                run, workers = await self._service.read_snapshot(parent, policy, run.id)
                view = self._render(run, workers)

                async def edit_current(
                    message: discord.Message,
                    *,
                    view: discord.ui.LayoutView,
                    allowed_mentions: discord.AllowedMentions | None = None,
                ) -> discord.Message | None:
                    self._message = message
                    await self._display(view)
                    return self._message

                await finish_control(
                    interaction,
                    view=view,
                    allowed_mentions=discord.AllowedMentions.none(),
                    edit=edit_current,
                )
                return True
            except (FleetRefused, WorkerSpecRefused) as refused:
                await interaction.response.send_message(
                    _refusal_text(refused, action), ephemeral=True
                )
                return False

    async def allow_sealed_responses(self) -> bool:
        """Only the trusted snapshot's proven absence restores ordinary delivery.

        An unresolved or foreign root never permits disclosure. Active fleet
        text is governed by collect/review, including on terminal failure.
        """
        async with self._lock:
            try:
                return await self._snapshot(None) is None
            except (FleetRefused, WorkerSpecRefused):
                return False

    async def allow_summary(self) -> bool:
        """Compatibility gate for callers that only need disclosure authority."""
        allowed, _ = await self.summary_gate(render=self._message is not None)
        return allowed

    async def summary_gate(
        self, *, render: bool = True, seen: bool = True
    ) -> tuple[bool, str | None]:
        """Decide what the turn's final answer may show, and end the card with it.

        `seen` says the root's answer follows a collect that returned the run's
        end (see `fleet_result_seen`). (True, None): deliver the root's answer (no
        fleet, a seen result, or a run that never started a worker). (True,
        notice): a partial result; the notice heads the root's answer. (False,
        notice): the notice replaces it. A result the root has not seen is owed
        to a wake turn, which delivers it; the run ends only after that.
        """
        async with self._lock:
            try:
                snapshot = await self._snapshot(None)
                if snapshot is None:
                    return True, None
                parent, policy, run, workers = snapshot
                if not workers:
                    # The root started nothing, so its answer carries no worker output.
                    run = await self._settle(parent, policy, run)
                    if render and self._message is not None:
                        await self._display(self._render(run, workers))
                    return True, None
                reply = await self._service.collect(parent, policy, run_id=run.id)
                run_id = run.id
                ready = reply.get("status") == "ready"
                blocked = reply.get("status") == "blocked"
                partial = blocked and reply.get("partial") is True
                # An answer that predates the result leaves it to a wake turn.
                deferred = (
                    (ready or blocked)
                    and not seen
                    and self._wake_id is None
                    and await self._service.defer(parent, policy, run_id=run_id)
                )
                if deferred:
                    ready = blocked = partial = False
                elif ready or blocked:
                    run = await self._settle(parent, policy, run, unless_waking=not seen)
            except (FleetRefused, WorkerSpecRefused):
                return False, _UNAVAILABLE
            if render:
                # Collect can observe rows newer than the initial snapshot.
                try:
                    run, workers = await self._service.read_snapshot(parent, policy, run_id)
                    view = self._render(
                        run,
                        workers,
                        collected=ready,
                        blocked=blocked and not partial,
                        partial=partial,
                    )
                except (FleetRefused, WorkerSpecRefused):
                    view = None
                if view is not None and (ready or blocked or self._message is not None):
                    await self._display(view)
            details = _worker_list(workers)
            if deferred:
                return False, _FINISHED
            if partial:
                done = sum(1 for row in workers if row.status == "succeeded")
                return True, (
                    f"Partial result: {done} of {len(workers)} workers succeeded. "
                    f"Fleet workers: {details}."
                )
            if blocked:
                notice = "Fleet run didn't finish. "
                if details:
                    notice += details + ". "
                return False, notice + "No reviewed result to share."
            if ready:
                return True, None
            # No per-worker states here: this message is never edited again, so
            # they would read as stuck after the run ends (r14). The card above is
            # the live progress, and it ends in the run's final state.
            return False, (
                "Fleet is still working. The fleet card shows each worker as it "
                "finishes, and I'll post the result here when they're done."
            )

    async def end_after_error(self) -> None:
        """Leave a failed root turn's card current, and still owe the root its result.

        A run with no worker ends. Running workers are armed by collect; finished
        ones are owed a wake unless this is that wake. Nothing is disclosed here.
        """
        async with self._lock:
            try:
                snapshot = await self._snapshot(None)
                if snapshot is None:
                    return
                parent, policy, run, workers = snapshot
                if not workers:
                    run = await self._settle(parent, policy, run)
                    if self._message is not None:
                        await self._display(self._render(run, workers))
                    return
                reply = await self._service.collect(parent, policy, run_id=run.id)
                if reply.get("status") in ("ready", "blocked") and self._wake_id is None:
                    await self._service.defer(parent, policy, run_id=run.id)
                run, workers = await self._service.read_snapshot(parent, policy, run.id)
                await self._display(self._render(run, workers))
            except (FleetRefused, WorkerSpecRefused) as error:
                log.info("fleet.card_error_end_skipped", reason=str(error))

    async def _settle(
        self,
        parent: WorkerPermissions,
        policy: TenantAccessPolicy,
        run: FleetRun,
        *,
        unless_waking: bool = True,
    ) -> FleetRun:
        """End a run left with no active worker; a lost race keeps the read row."""
        try:
            settled = await self._service.settle(
                parent,
                policy,
                run_id=run.id,
                wake_id=self._wake_id,
                unless_waking=unless_waking,
            )
        except (FleetRefused, WorkerSpecRefused) as error:
            log.info("fleet.run_settle_skipped", run_id=str(run.id), reason=str(error))
            return run
        return settled or run
