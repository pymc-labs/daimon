"""Discord rendering over the shared, authenticated fleet operations.

The host supplies its current-root loader. A card handle never supplies root
authority, and an operation acknowledgement never certifies native completion.
"""

from __future__ import annotations

import asyncio
import re
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Self, cast

from daimon.adapters.discord.posted_controls.view import build_card_view
from daimon.adapters.discord.views import finish_control
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.fleet import TERMINAL, FleetRun, FleetWorker
from daimon.core.fleet_orchestration import Control, FleetOrchestrator, FleetRefused
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.neutral_resources import WorkerPermissions, WorkerSpecRefused
from daimon.core.posted_controls import CardButton, CardState, PostedCard
from daimon.core.stores.agent_posts import AgentPostRow

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

_MESSAGE_TEXT_LIMIT = 4000


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
) -> discord.ui.LayoutView:
    """Render only canonical scoped rows; no specs, excerpts or native handles."""
    if (
        run.platform != "discord"
        or len({worker.id for worker in workers}) != len(workers)
        or len(workers) > run.max_workers
        or any(worker.run_id != run.id or worker.tenant_id != run.tenant_id for worker in workers)
    ):
        raise FleetRefused("foreign_worker")
    state: CardState = (
        "applied"
        if collected
        else "refused"
        if run.status in ("cancelled", "failed")
        else "received"
    )
    facts = tuple(
        f"{worker.ordinal}. **{_literal(worker.role, 60)}** · "
        f"{_literal(worker.backend, 20)} / {_literal(worker.model, 40)} — "
        f"{worker.status.replace('_', ' ')}"
        for worker in sorted(workers, key=lambda row: row.ordinal)
    )
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
        headline="Fleet — collected" if collected else f"Fleet — {run.status}",
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

    @classmethod
    def from_runtime(cls, service: FleetOrchestrator, deps: TurnDeps) -> Self:
        """Bind the real intent/root owner, retaining its original scope."""
        from daimon.core.channel_admins import load_live_subject, load_stored_subject
        from daimon.core.mux_backend import resource_scope
        from daimon.core.stores.identity import find_platform_principal
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
            from daimon.adapters.discord.checks import channel_admin_caller

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
                            caller=channel_admin_caller(interaction.user),
                        )
                    )
                result = await read_frozen_root(
                    deps,
                    resource_scope(
                        tenant_id=str(tenant_id),
                        account_id=str(original.account_id),
                        authorization_id="discord-fleet-card",
                    ),
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
        )
        return card


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
        return parent, policy, run, workers

    def _render(
        self, run: FleetRun, workers: tuple[FleetWorker, ...], *, collected: bool = False
    ) -> discord.ui.LayoutView:
        return build_fleet_view(
            run,
            workers,
            collected=collected,
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
            except (FleetRefused, WorkerSpecRefused):
                await interaction.response.send_message(
                    "This control is no longer available. Refresh the fleet card.",
                    ephemeral=True,
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
        """Gate the existing orchestrator answer on authoritative collect/review."""
        async with self._lock:
            try:
                snapshot = await self._snapshot(None)
                if snapshot is None:
                    return True
                parent, policy, run, workers = snapshot
                reply = await self._service.collect(parent, policy, run_id=run.id)
            except (FleetRefused, WorkerSpecRefused):
                return False
            ready = reply.get("status") == "ready"
            if ready and self._message is not None:
                view = self._render(run, workers, collected=True)
                await self._display(view)
            return ready
