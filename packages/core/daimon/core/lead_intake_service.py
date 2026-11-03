"""Durable intake application coordinator; provider I/O is supplied through ports."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Protocol
from uuid import UUID

from daimon.core.lead_intake import (
    IntakeBusy,
    IntakeConfig,
    IntakeRecord,
    IntakeResolution,
    LeadDraft,
    LeadEvidence,
    LeadSource,
    LeadSourceKind,
    LeadSubmission,
    captured_here,
    split_dossier,
    verify_lead_evidence,
)
from daimon.core.lead_intake_access import (
    LeadReadTarget,
    actual_thread_id,
    discovery_target,
    read_target,
)
from daimon.core.stores.lead_intake import IntakeStore
from pydantic import BaseModel


class VerifiedSource(BaseModel):
    source: LeadSource
    text: str
    recipients: tuple[str, ...] = ()
    # Reconstructed headers/text only; attachments are never retained.
    raw: str | None = None


class ReviewRequired(Exception):
    """A human must select records or reconcile an uncertain write."""


class EffectRequest(BaseModel):
    step: str
    key: str
    payload: dict[str, str]


class IntakePorts(Protocol):
    def replay_safe(self, effect: EffectRequest) -> bool: ...

    async def source(
        self, config: IntakeConfig, source: LeadSource, actor_id: str | None
    ) -> VerifiedSource: ...
    async def recover(self, config: IntakeConfig, effect: EffectRequest) -> str | None: ...
    async def write(self, config: IntakeConfig, effect: EffectRequest) -> str: ...
    async def scan(
        self, config: IntakeConfig, start: datetime, end: datetime, cursor: str | None
    ) -> tuple[list[str], str | None]: ...


class IntakeAccess(BaseModel):
    source: LeadSource | None = None
    thread_id: str | None = None
    write: bool = True
    create_thread: bool = False


type Reauthorize = Callable[[IntakeAccess], Awaitable[None]]
# `authorize_lead_read` for the calling reader; raises when the read is refused.
type AuthorizeRead = Callable[[LeadReadTarget], Awaitable[None]]


class IntakeService:
    def __init__(
        self,
        store: IntakeStore,
        ports: IntakePorts,
        reauthorize: Reauthorize,
        *,
        authorize_read: AuthorizeRead,
        provenance: tuple[UUID, UUID] | None = None,
    ) -> None:
        self.store, self.ports, self.reauthorize = store, ports, reauthorize
        self.authorize_read = authorize_read
        self.provenance = provenance

    async def read_source(self, message_id: str) -> VerifiedSource:
        """Fetch an unexpired discovery for classification without retaining it."""
        config, _ = await self.store.configuration()
        source = LeadSource(
            kind=LeadSourceKind.GMAIL, mailbox=config.gmail_mailbox, message_id=message_id
        )
        await self.authorize_read(discovery_target(config, self.store.tenant_id))
        if not await self.store.is_discovered(config, message_id):
            raise ValueError("unexpired_discovery_required")
        verified = await self.ports.source(config, source, None)
        if verified.source != source:
            raise ValueError("source_identity_mismatch")
        return verified.model_copy(update={"raw": None})

    async def submit(self, draft: LeadDraft, actor_id: str | None) -> IntakeRecord:
        await self.reauthorize(IntakeAccess(source=draft.source))
        config, _ = await self.store.configuration()
        verified = await self.ports.source(config, draft.source, actor_id)
        if verified.source != draft.source:
            raise ValueError("source_identity_mismatch")
        if (
            draft.source.kind == "gmail"
            and draft.classification != "other"
            and not (
                {x.casefold() for x in verified.recipients}
                & {x.casefold() for x in config.recipients}
            )
        ):
            raise ValueError("recipient_mismatch")
        evidence = LeadEvidence(
            original_text=verified.text,
            raw_source=verified.raw,
            **draft.model_dump(
                exclude={
                    "source",
                    "classification",
                    "opportunity_description",
                    "thread_title",
                    "thread_content",
                }
            ),
        )
        if (
            not config.attribution_property
            or not evidence.attribution_evidence
            or evidence.attribution_evidence not in evidence.original_text
            or (evidence.attribution_value or "").casefold()
            not in evidence.attribution_evidence.casefold()
        ):
            evidence = evidence.model_copy(
                update={"attribution_value": None, "attribution_evidence": None}
            )
        evidence = verify_lead_evidence(evidence)
        submission = LeadSubmission(
            source=verified.source,
            evidence=evidence,
            classification=draft.classification,
            opportunity_description=draft.opportunity_description,
            thread_title=(
                evidence.company_name or evidence.enquirer_name or draft.thread_title
            ).strip(),
            thread_content=draft.thread_content,
        )
        record = await self.store.accept(config, submission, actor_id, self.provenance)
        if draft.source.kind == "gmail":
            async with self.store.claim(f"mailbox:{self.store.mailbox_key(config)}"):
                state = await self.store.mailbox(config)
                state.discovered = [x for x in state.discovered if x != draft.source.message_id]
                await self.store.save_mailbox(config, state)
        return await self.resume(record.id)

    async def _effect(
        self,
        config: IntakeConfig,
        record: IntakeRecord,
        step: str,
        payload: dict[str, str],
        identity: str | None = None,
    ) -> str:
        if step == "starter":
            async with self.store.claim(f"discord-channel:{config.channel_id}"):
                return await self._apply_effect(config, record, step, payload, identity)
        return await self._apply_effect(config, record, step, payload, identity)

    async def _apply_effect(
        self,
        config: IntakeConfig,
        record: IntakeRecord,
        step: str,
        payload: dict[str, str],
        identity: str | None = None,
    ) -> str:
        # Checked again immediately before the write.
        access = IntakeAccess(
            source=record.submission.source if record.submission else None,
            thread_id=payload.get("thread") or record.progress.discord_thread_id,
            create_thread=step == "thread",
        )
        await self.reauthorize(access)
        live_record = await self.store.get(record.id)
        if live_record.submission is None or record.expires_at <= datetime.now(UTC):
            raise ValueError("intake_erased_or_expired")
        current, _ = await self.store.configuration()
        if current != config:
            raise ValueError("configuration_changed")
        if not captured_here(record, config):
            raise ValueError("destination_changed")
        key = hashlib.sha256(
            (
                f"{record.tenant_id}:{config.crm_connection_id}:{config.portal_id}:"
                f"{identity or record.id}:{step}"
            ).encode()
        ).hexdigest()
        saved = await self.store.effect(record.id, step)
        payload = {
            **payload,
            "_uncertain": str(saved is not None),
            "_accepted_at": record.created_at.isoformat(),
        }
        if saved is not None and step == "starter":
            claimed, peer = await self.store.starter_recovery_facts(record)
            payload.update(_claimed=json.dumps(claimed), _uncertain_peer=str(peer))
        if step.startswith("dossier:") and record.submission:
            index = int(step.split(":")[1])
            payload["_prefix"] = json.dumps(
                split_dossier(record.submission.thread_content)[: index + 1]
            )
        request = EffectRequest(step=step, key=key, payload=payload)
        if saved and saved.state == "completed":
            if payload.get("selected") and saved.remote_id != payload["selected"]:
                raise ReviewRequired("approved_selection_conflicts_with_recovery")
            return saved.remote_id or ""
        recovered = await self.ports.recover(config, request)
        if recovered is not None:
            if payload.get("selected") and recovered != payload["selected"]:
                raise ReviewRequired("approved_selection_conflicts_with_recovery")
            await self.store.intent(record.id, step, key)
            await self.store.finish(record.id, step, recovered)
            return recovered
        if saved and (saved.stable_key != key or not self.ports.replay_safe(request)):
            # Empty history is not proof a non-idempotent POST did not happen.
            raise ReviewRequired("uncertain_write")
        await self.store.intent(record.id, step, key)
        await self.reauthorize(access)
        remote_id = await self.ports.write(config, request)
        await self.store.finish(record.id, step, remote_id)
        return remote_id

    async def _authorize_effects(self, record: IntakeRecord) -> None:
        """Write authority over the record's actual destination and dossier thread."""
        await self.reauthorize(
            IntakeAccess(
                source=record.submission.source if record.submission else None,
                thread_id=actual_thread_id(record),
            )
        )

    async def get(self, intake_id: UUID) -> IntakeRecord:
        record = await self.store.get(intake_id)
        await self.authorize_read(read_target(record))
        return record

    async def advance(self, intake_id: UUID) -> IntakeRecord:
        """Run what effects remain; the result is not yet authorized for reading."""
        try:
            return await self._resume(intake_id)
        except IntakeBusy:
            return await self.store.get(intake_id)

    async def resume(self, intake_id: UUID) -> IntakeRecord:
        record = await self.advance(intake_id)
        await self.authorize_read(read_target(record))
        return record

    async def _resume(self, intake_id: UUID) -> IntakeRecord:
        await self.reauthorize(IntakeAccess())
        await self.store.purge(datetime.now(UTC))
        async with self.store.claim(f"source:{intake_id}"):
            record = await self.store.get(intake_id)
            config, _ = await self.store.configuration()
            if record.status in {"completed", "rejected", "expired"} or config.mode == "capture":
                return record
            if record.config_revision != config.revision or not captured_here(record, config):
                # Target/connection changes cannot silently redirect accepted evidence.
                record.error_code = "config_revision_review"
                await self.store.save(record)
                return record
            await self._authorize_effects(record)
            submission = record.submission
            if submission is None:
                return record
            if (
                submission.source.kind == "gmail"
                and (submission.source.mailbox or "").casefold() != config.gmail_mailbox.casefold()
            ):
                record.error_code = "configured_mailbox_mismatch"
                await self.store.save(record)
                return record
            p = record.progress
            try:
                p.discord_message_id = await self._effect(
                    config, record, "starter", {"content": submission.thread_title}
                )
                p.discord_thread_id = await self._effect(
                    config,
                    record,
                    "thread",
                    {"starter": p.discord_message_id, "title": submission.thread_title},
                )
                chunks = split_dossier(submission.thread_content)
                if not chunks:
                    raise ReviewRequired("dossier_required")
                for index, chunk in enumerate(chunks):
                    await self._effect(
                        config,
                        record,
                        f"dossier:{index}",
                        {"thread": p.discord_thread_id, "content": chunk},
                    )
                    p.discord_dossier_chunks_posted = index + 1
                if submission.source.kind == "gmail":
                    await self._effect(
                        config, record, "archive", {"message": submission.source.message_id}
                    )
                if config.mode == "crm_paused":
                    record.status = "pending"
                    await self.store.save(record)
                    return record
                e = submission.evidence
                if not e.identity_verified and not (
                    p.selected_company_id and p.selected_contact_id
                ):
                    raise ReviewRequired("identity_review")
                company_key = e.company_domain or f"selected:{p.selected_company_id}"
                async with self.store.claim(
                    f"company:{config.crm_connection_id}:{config.portal_id}:{company_key}"
                ):
                    p.company_id = await self._effect(
                        config,
                        record,
                        "company",
                        {
                            "name": e.company_name or "",
                            "domain": e.company_domain or "",
                            "selected": p.selected_company_id or "",
                        },
                        company_key,
                    )
                    # Aliases resolving to one company share the opportunity claim.
                    async with self.store.claim(
                        f"crm-company:{config.crm_connection_id}:{config.portal_id}:{p.company_id}"
                    ):
                        p.contact_id = await self._effect(
                            config,
                            record,
                            "contact",
                            {
                                "email": e.enquirer_email or "",
                                "selected": p.selected_contact_id or "",
                            },
                            e.enquirer_email,
                        )
                        deal_identity = await self.store.opportunity(
                            config, record, p.company_id, submission.opportunity_description
                        )
                        p.deal_id = await self._effect(
                            config,
                            record,
                            "deal",
                            {
                                "attribution_value": e.attribution_value or "",
                                "description": submission.opportunity_description,
                                "name": (
                                    f"{e.company_name or 'Lead'}: "
                                    f"{submission.opportunity_description}"
                                )[:200],
                                "company": p.company_id,
                                "contact": p.contact_id,
                                "domain": e.company_domain or "",
                                "company_name": e.company_name or "",
                                "selected": p.selected_deal_id or "",
                                "separate": str(p.separate_opportunity_confirmed),
                                "thread_url": f"https://discord.com/channels/{config.guild_id}/{p.discord_thread_id}",
                            },
                            deal_identity,
                        )
                        await self.store.finish_opportunity(
                            deal_identity,
                            p.company_id,
                            submission.opportunity_description,
                            p.deal_id,
                        )
                        for entity in ("company", "contact", "deal"):
                            selected = getattr(p, f"selected_{entity}_id")
                            if selected and selected != getattr(p, f"{entity}_id"):
                                raise ReviewRequired("approved_selection_conflicts_with_recovery")
                        for kind, remote in (
                            ("companies", p.company_id),
                            ("contacts", p.contact_id),
                        ):
                            await self._effect(
                                config,
                                record,
                                f"association:{kind}",
                                {"deal": p.deal_id, "target": remote},
                            )
                record.status, record.error_code = "completed", None
                p.review_reason = None
            except ReviewRequired as exc:
                record.status, record.error_code = "review", str(exc)
                p.review_reason = record.error_code
            except Exception:
                # Provider bodies can contain customer data and credentials.
                record.status, record.error_code = "pending", "provider_unavailable"
            await self.store.save(record)
            return record

    async def resolve(
        self, intake_id: UUID, resolution: IntakeResolution, actor_id: str
    ) -> IntakeRecord:
        await self.reauthorize(IntakeAccess())
        async with self.store.claim(f"source:{intake_id}"):
            record = await self.store.get(intake_id)
            # A resolver reads the record they decide on; may_resolve is live.
            await self.authorize_read(read_target(record))
            if record.status not in {"pending", "review"}:
                raise ValueError("intake_not_resolvable")
            current_config, _ = await self.store.configuration()
            if record.config_revision != current_config.revision or not captured_here(
                record, current_config
            ):
                raise ValueError("config_revision_review")
            p = record.progress
            await self._authorize_effects(record)
            before = p.model_dump_json()
            starter_resolution: tuple[str, str | None] | None = None
            if resolution.deal_id and resolution.separate_opportunity_confirmed:
                raise ValueError("conflicting_resolution")
            if resolution.discord_message_id or resolution.discord_post_absence_confirmed:
                if resolution.discord_message_id and resolution.discord_post_absence_confirmed:
                    raise ValueError("conflicting_discord_resolution")
                intent = await self.store.effect(intake_id, "starter")
                if intent is None or intent.state == "completed" or record.submission is None:
                    raise ValueError("uncertain_starter_required")
                config, _ = await self.store.configuration()
                await self._authorize_effects(record)
                check = EffectRequest(
                    step="starter_resolution",
                    key=intent.stable_key,
                    payload={
                        "content": record.submission.thread_title,
                        "selected": resolution.discord_message_id or "",
                        "accepted_at": record.created_at.isoformat(),
                    },
                )
                recovered = await self.ports.recover(config, check)
                if resolution.discord_message_id:
                    if recovered is None or recovered != resolution.discord_message_id:
                        raise ValueError("invalid_starter_selection")
                    starter_resolution = ("starter", recovered)
                    p.discord_message_id = recovered
                else:
                    if datetime.now(UTC) - intent.created_at < timedelta(minutes=10):
                        raise ValueError("absence_requires_ten_minutes")
                    if recovered != "absent":
                        raise ValueError("starter_candidates_require_selection")
                    starter_resolution = ("starter", None)
            for step in ("company", "contact", "deal"):
                selected = getattr(resolution, f"{step}_id")
                effect = await self.store.effect(intake_id, step)
                if (
                    effect
                    and effect.state != "completed"
                    and (selected or (step == "deal" and resolution.separate_opportunity_confirmed))
                ):
                    raise ValueError("recover_uncertain_create_first")
                completed = getattr(p, f"{step}_id")
                if completed and selected and completed != selected:
                    raise ValueError("completed_identity_immutable")
                if selected:
                    setattr(p, f"selected_{step}_id", selected)
            p.separate_opportunity_confirmed = resolution.separate_opportunity_confirmed
            p.resolved_by, p.resolution_evidence = actor_id, resolution.evidence
            p.review_reason, record.error_code = None, None
            await self.authorize_read(read_target(record))
            await self.store.audit(
                record, actor_id, before, p.model_dump_json(), effect_resolution=starter_resolution
            )
        return await self.resume(intake_id)

    async def scan(self) -> dict[str, object]:
        await self.reauthorize(IntakeAccess())
        config, _ = await self.store.configuration()
        await self.authorize_read(discovery_target(config, self.store.tenant_id))
        async with self.store.claim(f"mailbox:{self.store.mailbox_key(config)}"):
            state = await self.store.mailbox(config)
            anchor = state.checkpoint or config.reconciliation_start
            now = datetime.now(UTC)
            end = min(now, anchor + timedelta(days=7))
            start = max(config.reconciliation_start, anchor - timedelta(days=2))
            scan_id = await self.store.start_scan(config, start, end)
            discovered_count = 0
            cursor: str | None = None
            seen: set[str] = set()
            try:
                while True:
                    ids, cursor = await self.ports.scan(config, start, end, cursor)
                    new_ids = [
                        message
                        for message in ids
                        if not await self.store.seen_source(config, message)
                    ]
                    await self.store.record_discovered(config, new_ids, scan_id)
                    discovered_count += len(new_ids)
                    state.discovered = list(dict.fromkeys([*state.discovered, *new_ids]))
                    await self.store.save_mailbox(config, state)
                    if not cursor:
                        break
                    if cursor in seen:
                        raise ValueError("repeated_cursor")
                    seen.add(cursor)
                state.checkpoint, state.error_code = end, None
            except Exception:
                state.error_code = "mailbox_scan_failed"
            await self.store.finish_scan(scan_id, discovered_count, state.error_code)
            await self.store.save_mailbox(config, state)
            return dict(
                checkpoint=state.checkpoint,
                discovered=state.discovered[:100],
                has_more_discovered=len(state.discovered) > 100,
                caught_up=state.error_code is None and end == now,
                error_code=state.error_code,
            )
