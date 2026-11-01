"""Web chat turns: admitted like any chat's, run apart from the request that started them,
and streamed to whoever is watching.

One turn per chat at a time. A web chat sits in its person's own place (`place_of`),
seen only by them; its thread is `web:<chat id>`.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import anthropic
import structlog
from daimon.adapters.web.cards import WebCards
from daimon.adapters.web.credential_cards import WebCredentialCards
from daimon.adapters.web.files import attachment_prompt, keep_output, seal_attachment
from daimon.adapters.web.lifecycle import WebTurnLifecycle
from daimon.adapters.web.refusals import busy, failed, refusal_for
from daimon.adapters.web.stream import StreamRegistry, TurnStream
from daimon.adapters.web.stream_events import ReplyView
from daimon.core.anthropic_guard import guard_selfhosted_channel
from daimon.core.authz import Surface
from daimon.core.config import Settings, WebSettings
from daimon.core.direct_messages import bounded_turns
from daimon.core.errors import UserFacingError
from daimon.core.handoff_context import TranscriptTurn, render_previous_session
from daimon.core.ma_resolver import MAResolverMissError
from daimon.core.observability import capture_exception_with_scope
from daimon.core.output_delivery import (
    DeliverableFile,
    OutputPostingUnavailable,
    SkippedFile,
    sweep_session_outputs,
)
from daimon.core.stores import file_uploads
from daimon.core.stores import web_chat as chat_store
from daimon.core.stores.domain import (
    WebCard,
    WebMessageBlocks,
    WebMessageRow,
    WebMessageStatus,
    WebOutputFile,
    WebPersonRow,
    WebRefusalCard,
    WebThreadRow,
)
from daimon.core.stores.tenants import get_turn_cap
from daimon.core.stores.thread_sessions import clear_active_turn, mark_turn_active
from daimon.core.stores.turn_card_intents import (
    create_turn_card_intent,
    record_turn_card_message,
    retire_turn_card_intent,
)
from daimon.core.turn import turn_deadline
from daimon.core.turn.admission import (
    Admission,
    AdmissionDenied,
    MissingTurnConfigError,
    admit,
    backend_facts,
)
from daimon.core.turn.deps import TurnDeps
from daimon.core.turn.errors import SessionAgentMismatch, SessionBusyError, SessionPreparationFailed
from daimon.core.turn.outcomes import (
    current_outcome,
    finish_with_error,
    observe_turn,
    record_refusal,
)
from daimon.core.turn.prepare import bind_session
from daimon.core.turn.run import run_prepared_turn
from daimon.core.turn.slots import holding, release_turn_slot, wait_for_slot
from daimon.core.turn.state import ToolUseBlock
from daimon.core.turn_origin import render_turn_origin, turn_origin
from daimon.core.turn_queue import TurnQueue
from daimon.core.web_identity import WebPrincipal, place_of, thread_key, with_spend_person
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

log = structlog.get_logger(__name__)

_BIND_REFUSALS = (SessionPreparationFailed, SessionBusyError, SessionAgentMismatch)


class WebTurns:
    """The web process's running turns: their streams, stop switches and slots."""

    def __init__(self, settings: Settings, web: WebSettings, deps: TurnDeps) -> None:
        self.web = web
        self.deps = deps
        self.queue = TurnQueue.from_settings(settings.turn_queue, platform="web")
        self.streams = StreamRegistry()
        self.cards = WebCards(deps.sessionmaker)
        # The live reply of each thread with a running turn, for cards raised from outside.
        self.lifecycles: dict[uuid.UUID, WebTurnLifecycle] = {}
        self.credentials = WebCredentialCards(
            deps.sessionmaker, deps.anthropic, self.lifecycles.get
        )
        self.draining = False
        self._cancels: dict[uuid.UUID, asyncio.Event] = {}
        self._tasks: set[asyncio.Task[None]] = set()

    @property
    def sessionmaker(self) -> async_sessionmaker[AsyncSession]:
        return self.deps.sessionmaker

    def spawn(self, coro: Coroutine[Any, Any, None]) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def start(
        self,
        *,
        person: WebPersonRow,
        principal: WebPrincipal,
        thread: WebThreadRow,
        message: WebMessageRow,
    ) -> TurnStream:
        """Run a turn answering `message`; its stream at once, before it is admitted."""
        stream = self.streams.open(thread.id, message.id)
        self.spawn(self._run(person, principal, thread, message, stream))
        return stream

    async def stop(self, thread_id: uuid.UUID, clicker: str) -> bool:
        """Stop the running turn; a card it waits on is answered Deny so it can end."""
        cancel = self._cancels.get(thread_id)
        if cancel is None:
            return False
        cancel.set()
        await self.cards.deny_open(thread_id, clicker)
        return True

    async def drain(self, timeout_s: float) -> None:
        """Let running turns finish, then stop the rest."""
        self.draining = True
        await self.cards.deny_all_open()
        if self._tasks:
            await asyncio.wait(set(self._tasks), timeout=timeout_s)
        for cancel in self._cancels.values():
            cancel.set()
        if self._tasks:
            await asyncio.wait(set(self._tasks), timeout=10)
        for task in set(self._tasks):
            task.cancel()

    async def _run(
        self,
        person: WebPersonRow,
        principal: WebPrincipal,
        thread: WebThreadRow,
        message: WebMessageRow,
        stream: TurnStream,
    ) -> None:
        turn_id, key, place = uuid.uuid4(), thread_key(thread.id), place_of(thread.person_id)

        async def save(
            reply_id: uuid.UUID,
            view: ReplyView,
            status: WebMessageStatus,
            card: WebCard | None,
            ms: int | None,
            files: tuple[WebOutputFile, ...] = (),
            cost_usd: Decimal | None = None,
        ) -> WebMessageRow:
            blocks = WebMessageBlocks(steps=view.steps, files=files, card=card)
            async with self.sessionmaker.begin() as session:
                row = await chat_store.upsert_assistant_message(
                    session,
                    tenant_id=thread.tenant_id,
                    thread_id=thread.id,
                    message_id=reply_id,
                    turn_id=turn_id,
                    content=view.text,
                    blocks=blocks,
                    status=status,
                    cost_usd=cost_usd,
                    duration_ms=ms,
                )
                if row is not None:
                    await chat_store.update_thread(
                        session, thread_id=thread.id, updated_at=datetime.now(UTC)
                    )
            # The chat was deleted meanwhile: the reply is shown to whoever still watches
            # and kept nowhere, and the files stored for it go too.
            if row is None and files:
                async with self.sessionmaker.begin() as session:
                    await file_uploads.delete_unreferenced_web_uploads(
                        session, person_id=person.id, handle_ids=[file.id for file in files]
                    )
            return row or WebMessageRow(
                id=reply_id,
                tenant_id=thread.tenant_id,
                thread_id=thread.id,
                role="assistant",
                content=view.text,
                blocks=blocks,
                turn_id=turn_id,
                status=status,
                cost_usd=cost_usd,
                duration_ms=ms,
                feedback=None,
                seq=0,
                created_at=datetime.now(UTC),
            )

        lifecycle = self.lifecycles[thread.id] = WebTurnLifecycle(stream, save)
        cancel = self._cancels[thread.id] = asyncio.Event()
        try:
            await save(lifecycle.reply_id, ReplyView(), "running", None, None)
            cap = await get_turn_cap(
                self.sessionmaker,
                tenant_id=thread.tenant_id,
                default=self.web.max_concurrent_turns_per_tenant,
            )
            ticket = self.queue.admit(thread.tenant_id, cap=cap, conversation_id=key)
            if ticket is None:
                record_refusal(
                    self.sessionmaker,
                    tenant_id=thread.tenant_id,
                    platform="web",
                    channel_id=place,
                    thread_id=key,
                )
                await lifecycle.finish("done", card=busy())
                return
            with (
                holding(ticket),
                observe_turn(
                    self.sessionmaker,
                    tenant_id=thread.tenant_id,
                    platform="web",
                    channel_id=place,
                    thread_id=key,
                ),
            ):
                try:
                    async with guard_selfhosted_channel(
                        self.sessionmaker,
                        tenant_id=thread.tenant_id,
                        platform="web",
                        channel_id=place,
                        thread_id=key,
                        caller_id=principal.external_id,
                    ):
                        await self._admitted(person, principal, thread, message, lifecycle, cancel)
                finally:
                    release_turn_slot()
        except asyncio.CancelledError:
            await asyncio.shield(self._end(lifecycle, "stopped"))
            raise
        except Exception as exc:
            log.error("web.turn.failed", thread_id=str(thread.id), exc_info=exc)
            capture_exception_with_scope(exc)
            await self._end(lifecycle, "done", failed())
        finally:
            self._cancels.pop(thread.id, None)
            self.lifecycles.pop(thread.id, None)
            await stream.close()
            self.streams.close_later(thread.id, stream)

    async def _end(
        self,
        lifecycle: WebTurnLifecycle,
        status: WebMessageStatus,
        card: WebRefusalCard | None = None,
    ) -> None:
        if not lifecycle.finished:
            try:
                await lifecycle.finish(status, card=card)
            except Exception:
                log.exception("web.turn.finish_failed")

    async def _admitted(
        self,
        person: WebPersonRow,
        principal: WebPrincipal,
        thread: WebThreadRow,
        message: WebMessageRow,
        lifecycle: WebTurnLifecycle,
        cancel: asyncio.Event,
    ) -> None:
        deps, key, tenant_id = self.deps, thread_key(thread.id), thread.tenant_id
        place = place_of(thread.person_id)
        async with self.sessionmaker() as session:
            principal = await with_spend_person(session, person, principal)
        try:
            admission = await admit(
                deps,
                tenant_id=tenant_id,
                platform="web",
                external_user_id=principal.external_id,
                channel_id=place,
                thread_id=key,
                role=principal.role,
                now=datetime.now(UTC),
                requester_org=principal.org,
                requester_home_id=principal.home_id,
                surface=Surface.WEB,
                principal_platform=principal.platform,
                spend_person=principal.spend_person,
            )
        except (
            MissingTurnConfigError,
            MAResolverMissError,
            AdmissionDenied,
            UserFacingError,
        ) as err:
            await lifecycle.finish("done", card=refusal_for(err))
            return
        async with self.sessionmaker.begin() as session:
            intent = await create_turn_card_intent(
                session,
                tenant_id=tenant_id,
                platform="web",
                thread_id=key,
                turn_token=uuid.uuid4(),
                channel_id=key,
            )
            await record_turn_card_message(
                session, id=intent.id, message_id=str(lifecycle.reply_id)
            )
        markers: set[uuid.UUID] = set()
        try:
            slot = await wait_for_slot(
                cancel,
                sessionmaker=self.sessionmaker,
                tenant_id=tenant_id,
                default_cap=self.web.max_concurrent_turns_per_tenant,
            )
            if slot == "cancelled":
                await lifecycle.finish("stopped")
            elif slot in ("balance_depleted", "allowance_exhausted"):
                await lifecycle.finish("done", card=refusal_for(AdmissionDenied(reason=slot)))
            elif slot != "started":
                record_refusal(
                    self.sessionmaker,
                    tenant_id=tenant_id,
                    platform="web",
                    channel_id=place,
                    thread_id=key,
                )
                await lifecycle.finish("done", card=busy())
            else:
                await self._bind_and_run(
                    principal, thread, message, admission, lifecycle, cancel, markers
                )
        finally:
            async with self.sessionmaker.begin() as session:
                await retire_turn_card_intent(session, id=intent.id, expected_message_id=None)
                for mapping_id in markers:
                    await clear_active_turn(session, id=mapping_id)

    async def _bind_and_run(
        self,
        principal: WebPrincipal,
        thread: WebThreadRow,
        message: WebMessageRow,
        admission: Admission,
        lifecycle: WebTurnLifecycle,
        cancel: asyncio.Event,
        markers: set[uuid.UUID],
    ) -> None:
        deps, key, tenant_id = self.deps, thread_key(thread.id), thread.tenant_id
        place = place_of(thread.person_id)
        deadline = turn_deadline(now=datetime.now(UTC))
        try:
            prepared = await bind_session(
                deps,
                admission,
                tenant_id=tenant_id,
                platform="web",
                external_user_id=principal.external_id,
                thread_id=key,
                session_account_id=admission.account_id,
                reuse_existing=True,
                deadline=deadline,
            )
        except _BIND_REFUSALS as error:
            await lifecycle.finish("done", card=refusal_for(error))
            return
        if prepared.mapping_id is not None:
            async with self.sessionmaker.begin() as session:
                await mark_turn_active(
                    session,
                    id=prepared.mapping_id,
                    active_turn_message_id=str(lifecycle.reply_id),
                    active_turn_channel_id=key,
                    now=datetime.now(UTC),
                )
            markers.add(prepared.mapping_id)
        prefix, images = await attachment_prompt(
            self.sessionmaker,
            tenant_id=tenant_id,
            person_id=thread.person_id,
            files=message.blocks.attachments,
            link=self._link_for(tenant_id),
        )
        config = admission.config
        async with turn_origin(
            self.sessionmaker,
            tenant_id=tenant_id,
            account_id=admission.account_id,
            platform="web",
            parent_channel_id=place,
            thread_id=key,
            responder_ma_agent_id=str(admission.agent.id),
            responder_name=config.agent_name or admission.agent.name,
            role=principal.role,
            configuration_target_ma_agent_id=config.configuration_target_ma_agent_id,
            configuration_target_name=config.configuration_target_name,
            is_setup=config.thread_binding_kind == "setup",
            sender_foreign=admission.hold is not None and admission.hold.client,
            shared_channel=admission.shared_channel,
            message_text=message.content,
            platform_channel_id=key,
            platform_message_id=str(message.id),
        ) as origin:
            continued = prepared.continuity.state == "continued"
            controls = render_turn_origin(
                origin,
                **backend_facts(admission),
                session_state=None if continued else prepared.continuity.session_state(),
            )

            async def reseed() -> str:
                # A new session has seen nothing: replay this chat before the message.
                async with self.sessionmaker() as session:
                    earlier = await chat_store.list_messages(session, thread_id=thread.id)
                previous = [
                    TranscriptTurn(role="user" if m.role == "user" else "agent", text=m.content)
                    for m in earlier
                    if m.seq < message.seq and m.content
                ]
                history = render_previous_session(
                    bounded_turns(previous), from_agent_name=admission.agent.name
                )
                return f"{controls}\n{history}\n\n{prefix}{message.content}"

            outcome = await run_prepared_turn(
                deps,
                prepared,
                tenant_id=tenant_id,
                platform="web",
                thread_id=key,
                external_user_id=principal.external_id,
                user_message=f"{controls}\n\n{prefix}{message.content}",
                request_text=message.content,
                lifecycle=lifecycle,
                cancel=cancel,
                reseed_user_message=reseed,
                recovery_lifecycle=lambda fresh: self._recover(thread.id, fresh, lifecycle),
                image_blocks=images or None,
                render_interval_s=self.web.render_interval_s,
                deadline=deadline,
                confirm_write=self.cards.hook(
                    thread_id=thread.id, lifecycle=lambda: lifecycle, subject=principal.subject
                ),
                active_turn_message_id=str(lifecycle.reply_id),
            )
        if (observation := current_outcome.get()) is not None:
            lifecycle.cost_usd = observation.billed_cost(deps.markup)
        if self._cancels.get(thread.id, cancel).is_set():
            await lifecycle.finish("stopped")
        elif outcome.state.error is not None:
            log.error(
                "web.turn.errored",
                thread_id=str(thread.id),
                kind=outcome.state.error.kind,
                exc_info=outcome.state.error,
            )
            finish_with_error(outcome.state.error)
            await lifecycle.finish("done", card=failed())
        else:
            used_tools = any(isinstance(b, ToolUseBlock) for b in outcome.state.content)
            revision = admission.backend_revision
            if used_tools and (revision is None or revision.backend != "selfhosted"):
                lifecycle.files = await self._outputs(
                    outcome.ma_session_id, tenant_id, thread.person_id
                )
            await lifecycle.finish("done")

    def _link_for(self, tenant_id: uuid.UUID) -> Callable[[str], str] | None:
        fernet = self.deps.fernet
        if fernet is None:
            return None
        base = str(self.web.public_url).rstrip("/")

        def link(handle: str) -> str:
            sealed = seal_attachment(fernet, tenant_id=tenant_id, handle=handle)
            return f"{base}/api/chat/attachments/{sealed}"

        return link

    async def _outputs(
        self, session_id: str, tenant_id: uuid.UUID, person_id: uuid.UUID
    ) -> tuple[WebOutputFile, ...]:
        """The files the turn left in its session's outputs, kept for its reply."""
        kept: list[WebOutputFile] = []

        async def post(file: DeliverableFile) -> None:
            kept.append(
                await keep_output(
                    self.sessionmaker,
                    tenant_id=tenant_id,
                    person_id=person_id,
                    file=file,
                    now=datetime.now(UTC),
                )
            )

        async def skip(file: SkippedFile) -> None:
            log.info("web.output.too_large", size=file.size_bytes)

        try:
            await sweep_session_outputs(
                self.deps.anthropic,
                session_id=session_id,
                post=post,
                on_skip=skip,
                max_bytes=file_uploads.MAX_UPLOAD_BYTES,
            )
        except (anthropic.APIError, OutputPostingUnavailable) as err:
            # Files missed now stay listed; the session's next tool turn sweeps them.
            log.warning("web.output.sweep_failed", err_type=type(err).__name__)
        return tuple(kept)

    def _recover(
        self, thread_id: uuid.UUID, fresh: asyncio.Event, lifecycle: WebTurnLifecycle
    ) -> WebTurnLifecycle:
        """A replaced session keeps rendering into the same reply; Stop follows it."""
        self._cancels[thread_id] = fresh
        return lifecycle
