"""Composition and reconciliation used by the existing trusted processes."""

import hashlib
from datetime import UTC, datetime

import httpx
from anthropic import AsyncAnthropic
from cryptography.fernet import MultiFernet
from daimon.core.config import Settings
from daimon.core.recruitment import (
    Candidate,
    CandidateField,
    ChangeOperation,
    Extraction,
    ProposedChange,
    RecruitmentConfig,
    SourceMessage,
)
from daimon.core.recruitment_api import RecruitmentAPI, SyntheticRecruitmentTransport
from daimon.core.recruitment_discord import DiscordResourceMissing, RecruitmentDiscord
from daimon.core.recruitment_extraction import extract_candidate
from daimon.core.recruitment_service import RecruitmentService
from daimon.core.stores.recruitment import (
    RecruitmentBusy,
    RecruitmentStore,
    enrolled_tenants,
    purge_expired,
)
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

# Synthetic identities/keys survive client/tick boundaries within this process.
_SYNTHETIC = SyntheticRecruitmentTransport()


def tenant_config(settings: Settings, guild_id: str) -> RecruitmentConfig | None:
    tenant = settings.recruitment.tenants.get(guild_id)
    if tenant is None:
        return None
    revision = int(hashlib.sha256(tenant.model_dump_json().encode()).hexdigest()[:12], 16)
    return RecruitmentConfig(guild_id=guild_id, revision=revision, **tenant.model_dump())


def build_service(
    settings: Settings,
    store: RecruitmentStore,
    discord: RecruitmentDiscord,
    http: httpx.AsyncClient,
    anthropic: AsyncAnthropic,
) -> RecruitmentService:
    backend = settings.recruitment.backends.get(store.tenant_id)
    if not settings.recruitment.enabled:
        raise PermissionError("recruitment_disabled")
    if settings.recruitment.synthetic:
        base_url, key = "https://synthetic.invalid", SecretStr(str(store.tenant_id))
    elif backend and backend.base_url.startswith("https://") and backend.api_key.get_secret_value():
        base_url, key = backend.base_url, backend.api_key
    else:
        raise PermissionError("backend_unconfigured")
    identity = hashlib.sha256(f"{base_url}:{key.get_secret_value()}".encode()).hexdigest()

    async def authorize(actor: str, thread: str) -> None:
        config = await store.configuration()
        if not config or not config.enabled:
            raise PermissionError("recruitment_disabled")
        await discord.check_access(config, thread, actor)
        await discord.check_access(config, thread, await discord.bot_id(), recruiter=False)

    async def extract(
        messages: list[SourceMessage], primary: Candidate | None, options: dict[str, list[str]]
    ) -> Extraction:
        if settings.recruitment.synthetic:
            # QA never sends candidate text to either backend or model provider.
            import re

            changes: list[ProposedChange] = []
            latest = messages[-1]
            for field in ("phone", "position"):
                match = re.search(rf"Synthetic {field}: ([^\n]+)", latest.content, re.IGNORECASE)
                if match and primary:
                    changes.append(
                        ProposedChange(
                            field=CandidateField(field),
                            operation=ChangeOperation.SET,
                            value=match.group(1),
                            source_message_id=latest.id,
                            evidence=match.group(0),
                        )
                    )
            return Extraction(
                candidate=Candidate(name="Synthetic candidate"),
                summary="Synthetic QA review; no live effects.",
                changes=changes,
            )
        import asyncio

        async with asyncio.timeout(90):
            return await extract_candidate(
                anthropic, settings.recruitment.model, messages, primary, options
            )

    return RecruitmentService(
        store, RecruitmentAPI(http, base_url, key), extract, authorize, backend=identity
    )


def backend_client(settings: Settings) -> httpx.AsyncClient:
    _SYNTHETIC.fail = settings.recruitment.synthetic_failure_status or None
    return httpx.AsyncClient(
        transport=_SYNTHETIC if settings.recruitment.synthetic else None,
        timeout=30,
        follow_redirects=False,
    )


async def reconcile_thread(
    service: RecruitmentService, discord: RecruitmentDiscord, thread: str
) -> None:
    config = await service.config()
    await discord.check_access(config, thread, await discord.bot_id(), recruiter=False)
    state = await service.store.get(thread)
    if not state or state.phase in {"skipped", "expired"}:
        return
    for message_id, source_channel in await service.store.pending_enrichment(thread):
        try:
            source = await discord.message(config, source_channel, message_id)
        except DiscordResourceMissing:
            from daimon.core.stores.recruitment import invalidate_source

            await invalidate_source(
                service.store.sessions,
                service.store.tenant_id,
                source_channel,
                message_id,
                deleted=True,
            )
            continue
        if source is not None:
            await service.store.ingest(thread, source)
        else:
            await service.store.ignore_bot_source(thread, message_id)
    # Scan old pages even after a restart; initial review stays unconfirmable
    # until a complete history traversal has established the evidence boundary.
    old, before, _ = await discord.history(config, thread, before=state.scan_before)
    if state.scan_before is None:
        try:
            starter = await discord.message(config, state.parent_id, thread)
        except PermissionError:
            # Forum starters live in the thread, not the parent.
            starter = await discord.message(config, thread, thread)
        if starter:
            old.append(starter)
    seen = set(state.scan_seen) | {m.id for m in old}
    new, _, cursor = await discord.history(config, thread, after=state.cursor)
    if before is None:
        for message_id, _, deleted in await service.store.message_manifest(thread):
            if message_id not in seen and int(message_id) <= int(state.cursor) and not deleted:
                await service.delete_message(thread, message_id)
    await service.ingest(thread, old + new)
    async with service.store.claim(thread):
        current = await service.store.get(thread)
        if current is None or current.phase == "expired":
            return
        current.scan_before, current.cursor = before, str(max(int(state.cursor), int(cursor)))
        current.scan_seen = sorted(seen, key=int) if before else []
        if before is None:
            current.scan_complete = True
        await service.store.save(current, config)
    await service.deliver(thread)


async def reconcile_recruitment(
    sessions: async_sessionmaker[AsyncSession],
    settings: Settings,
    fernet: MultiFernet | None,
    anthropic: AsyncAnthropic,
) -> None:
    tenants = await enrolled_tenants(sessions)
    # Erasure is independent of feature enablement, backend availability and Discord.
    for tenant in tenants:
        await purge_expired(sessions, tenant, datetime.now(UTC))
    if fernet is None or not settings.recruitment.enabled or settings.discord is None:
        return
    async with httpx.AsyncClient() as discord_http, backend_client(settings) as api_http:
        discord = RecruitmentDiscord(discord_http, settings.discord.bot_token.get_secret_value())
        from daimon.core.ma_identity import derive_tenant_uuid

        for guild_id in settings.recruitment.tenants:
            tenant = derive_tenant_uuid(platform="discord", workspace_id=guild_id)
            store = RecruitmentStore(sessions, fernet, tenant, tenant_config(settings, guild_id))
            config = await store.configuration()
            if not config or not config.enabled:
                continue
            try:
                service = build_service(settings, store, discord, api_http, anthropic)
            except PermissionError:
                continue
            try:
                channels = await discord.active(config)
            except (PermissionError, httpx.HTTPError, ValueError):
                channels = []
            for channel in channels:
                try:
                    await service.enroll(channel.id, channel.parent_id or "", channel.name)
                except (PermissionError, ValueError, RecruitmentBusy):
                    continue
            for thread in await store.list_threads():
                try:
                    await reconcile_thread(service, discord, thread)
                except (PermissionError, httpx.HTTPError, ValueError, RecruitmentBusy):
                    # No content and no fallback destination. A recruiter can see
                    # the opaque paused item through the private status command.
                    async with store.claim(thread):
                        state = await store.get(thread)
                        if state and state.phase != "expired":
                            state.error = "thread_reconciliation_paused"
                            await store.save(state, config)
