"""Per-channel backend configuration: which provider, profile and model a channel's turns use.

Off by default twice over. With `DAIMON_TURN__CHANNEL_BACKENDS` unset, admission
never reads it. With it set, a channel nobody configured has no revision and
admits exactly as before. A channel's configuration is a chain of immutable
`mux.contracts.config.ConfigRevision`s in `channel_config_revision`; the
newest one applies.

A configured channel is checked at admission, after the config cascade and
before any provider call: its profile must meet what the configuration
requires (`mux.contracts.admission.admit`), and this release must be able to
run it. Anthropic Managed Agents uses the agent's own model, per caller or
shared (`daimon.core.shared_threads`). Explicit OpenAI persistent-workspace
channels use gpt-6-luna per caller and require an injected native host runtime;
anything else is refused visibly as
`backend_unsupported` rather than quietly run on Anthropic.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import uuid
from collections.abc import Awaitable, Callable, Iterator, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, cast

import structlog
from anthropic import AsyncAnthropic
from anthropic.types.beta import BetaManagedAgentsAgent
from daimon.core.access_policy import OPEN_ACCESS_POLICY
from daimon.core.authz import Action, Subject, authorize
from daimon.core.config import TurnSettings
from daimon.core.defaults.ma_index import find_agent_by_daimon_tag
from daimon.core.defaults.metadata import MA_METADATA_KEY_NAME, MA_METADATA_KEY_TENANT
from daimon.core.errors import DaimonError
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.mux_backend import resource_scope
from daimon.core.mux_compat import source_agent_record
from daimon.core.neutral_resources import AgentFleetGrant, NeutralResourceHost, ResolvedWorkerSpec
from daimon.core.pricing import MODEL_PRICING, ProviderPrice, list_price_preview
from daimon.core.stores import access_policy as policy_store
from daimon.core.stores import agent_backends, mux_state, security_audit
from daimon.core.stores.domain import (
    AgentBackendRevision,
    BackendAgent,
    CompatibilityItem,
    CompatibilityPreview,
    TenantBackendPolicy,
)
from mux.contracts.admission import Admission as BackendAdmission
from mux.contracts.admission import admit
from mux.contracts.config import (
    DEFAULT_BACKEND,
    BackendConfig,
    ConfigRevision,
    ResolvedBackend,
    resolve_default,
)
from mux.contracts.ids import PROVIDERS, ChannelRef, Provider
from mux.contracts.profile import SUPPORTED, Capability, Profile
from mux.contracts.resources import AgentSpec
from mux.errors import InvalidConfig, UnsupportedCapability
from mux.profiles import (
    RUNNABLE_PROFILES,
    backend_selection,
    get_profile,
    runnable_selection,
    shared_threads_available,
)
from mux.state.store import ConfigRevisionConflict
from pydantic import JsonValue
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

log = structlog.get_logger(__name__)
DEFAULT_BACKEND_LABEL = f"Use default ({DEFAULT_BACKEND.capitalize()})"


class BackendUnsupported(DaimonError):
    """The channel's backend configuration cannot run here.

    Admission turns it into `AdmissionDenied(reason="backend_unsupported")`.
    """


def channel_ref(tenant_id: uuid.UUID, platform: str, channel_id: str) -> ChannelRef:
    return ChannelRef(tenant_id=str(tenant_id), platform=platform, channel_id=channel_id)


async def current_backend(session: AsyncSession, channel: ChannelRef) -> ConfigRevision | None:
    """The channel's newest configuration revision, or None if it was never configured."""
    return await mux_state.latest_config_revision(session, channel, overrides_only=True)


async def current_backend_and_sharing(
    session: AsyncSession, channel: ChannelRef
) -> tuple[ConfigRevision | None, bool]:
    """The newest revision and whether any revision ever shared threads, in one read."""
    return await mux_state.latest_config_revision_and_sharing(session, channel, overrides_only=True)


async def set_channel_backend(
    session: AsyncSession, channel: ChannelRef, config: BackendConfig
) -> ConfigRevision:
    """Record `config` as the channel's configuration (a new revision if it changed).

    Raises `InvalidConfig` for a selection that does not hold together (a
    profile of another backend, a non-default backend without a model). Does
    not check that the profile meets it: that happens at admission, so a
    configuration can be written before the backend it names is runnable.
    """
    await policy_store.lock_policy_writes_exclusive(session, tenant_id=uuid.UUID(channel.tenant_id))
    resolved = resolve_default(config)
    get_profile(resolved.profile)
    latest = await current_backend(session, channel)
    if (
        latest is not None
        and latest.digest == resolved.content_digest()
        and await mux_state.config_origin(session, channel, latest.local) == "channel"
    ):
        return latest
    revision = ConfigRevision.create(
        channel, await mux_state.next_config_local(session, channel), resolved
    )
    return await mux_state.put_config_revision(session, revision)


async def clear_channel_backend(session: AsyncSession, channel: ChannelRef) -> ConfigRevision:
    """Clear the override while retaining an immutable default revision for thread pins."""
    await policy_store.lock_policy_writes_exclusive(session, tenant_id=uuid.UUID(channel.tenant_id))
    latest = await current_backend(session, channel)
    if (
        latest is not None
        and await mux_state.config_origin(session, channel, latest.local) == "channel_clear"
    ):
        return latest
    revision = ConfigRevision.create(
        channel, await mux_state.next_config_local(session, channel), resolve_default(None)
    )
    return await mux_state.put_config_revision(session, revision, origin="channel_clear")


def check_backend(
    revision: ConfigRevision, *, enabled_backends: tuple[Provider, ...] | None = None
) -> BackendAdmission:
    """Admit `revision`, or raise `BackendUnsupported`."""
    if enabled_backends is not None and not backend_is_enabled(revision.backend, enabled_backends):
        raise BackendUnsupported("This backend is not enabled for this workspace.")
    try:
        admission = admit(revision, get_profile(revision.profile))
    except (UnsupportedCapability, InvalidConfig) as err:
        log.info(
            "turn.backend_unsupported",
            channel_id=revision.channel.channel_id,
            profile=revision.profile,
            error=str(err),
        )
        raise BackendUnsupported(str(err)) from err
    runnable = revision.profile in RUNNABLE_PROFILES and runnable_selection(revision)
    if not runnable:
        log.info(
            "turn.backend_unsupported",
            channel_id=revision.channel.channel_id,
            profile=revision.profile,
            error="not runnable in this release",
        )
        raise BackendUnsupported(f"{revision.profile} is not runnable in this release")
    return admission


@dataclass(frozen=True)
class BackendActor:
    tenant_id: uuid.UUID
    account_id: uuid.UUID
    subject: Subject


@dataclass(frozen=True)
class BackendSelectionState:
    agent: BackendAgent
    revision: AgentBackendRevision | None
    policy: TenantBackendPolicy
    preview: CompatibilityPreview


@dataclass(frozen=True)
class BackendResolution:
    source: Literal["channel", "agent", "default"]
    selection: ResolvedBackend
    revision: ConfigRevision | None = None
    agent_revision: int | None = None


class AgentBackendRefused(DaimonError):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason.replace("_", " "))


def _digest(facts: object) -> str:
    return hashlib.sha256(
        json.dumps(facts, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def _revision_digest(
    agent: BackendAgent, backend: Provider | None, fleet_grant: JsonValue | None
) -> str:
    facts: dict[str, object] = {
        "tenant_id": agent.tenant_id,
        "agent_id": agent.agent_id,
        "backend": backend,
    }
    if fleet_grant is not None:
        facts["fleet_grant"] = fleet_grant
    return _digest(facts)


def spec_compatibility_items(
    spec: AgentSpec | BetaManagedAgentsAgent, *, skill_providers: tuple[Provider, ...] = PROVIDERS
) -> tuple[Iterator[CompatibilityItem], Iterator[CompatibilityItem]]:
    """Map source skills and MCP requirements once for preview and worker derivation."""
    if isinstance(spec, AgentSpec):
        skill_names = (pin.id for pin in spec.skills or ())
        servers = ((server.name, bool(server.credential_ref)) for server in spec.mcp_servers or ())
    else:
        skill_names = (pin.skill_id for pin in spec.skills or ())
        # The SDK does not expose vault credential intent; retain its conservative requirement.
        servers = ((server.name, True) for server in spec.mcp_servers or ())
    return (
        (
            CompatibilityItem(name=name, requires=("skills_bundle",), providers=skill_providers)
            for name in skill_names
        ),
        (
            CompatibilityItem(
                name=name,
                requires=("tool_loop", "required_actions", *(("vaults",) if credential else ())),
            )
            for name, credential in servers
        ),
    )


def compatibility_missing(
    item: CompatibilityItem, target_backend: Provider, profile: Profile
) -> bool:
    """One provider/capability predicate for preview and worker derivation."""
    return target_backend not in item.providers or any(
        profile.support_for(cap) not in SUPPORTED for cap in item.requires
    )


def compatibility_preview(
    agent: BackendAgent,
    target_backend: Provider,
    tenant: TenantBackendPolicy,
    *,
    setting_revision: int = 0,
    cost_line: str = "Cost preview pending",
) -> CompatibilityPreview:
    """Pure preview from host-resolved agent facts and declared profile metadata."""
    if agent.tenant_id != tenant.tenant_id:
        raise AgentBackendRefused("tenant_mismatch")
    selection = backend_selection(target_backend)
    profile = get_profile(selection.profile)

    preview = CompatibilityPreview(
        digest="",
        tenant_id=agent.tenant_id,
        agent_id=agent.agent_id,
        target_backend=target_backend,
        setting_revision=setting_revision,
        policy_revision=tenant.revision,
        unsupported_skills=tuple(
            sorted(
                i.name for i in agent.skills if compatibility_missing(i, target_backend, profile)
            )
        ),
        unsupported_mcps=tuple(
            sorted(i.name for i in agent.mcps if compatibility_missing(i, target_backend, profile))
        ),
        unsupported_capabilities=tuple(
            sorted(
                c
                for c in agent.required_capabilities
                if compatibility_missing(
                    CompatibilityItem(name=c, requires=(c,)), target_backend, profile
                )
            )
        ),
        shared_thread_limits=("Shared threads are unavailable on this selection.",)
        if not shared_threads_available(target_backend)
        else (),
        cost_line=cost_line,
        allowed=backend_is_enabled(target_backend, tenant.enabled_backends),
    )
    digest = _digest(
        {
            **preview.model_dump(mode="json"),
            "configuration_digest": agent.configuration_digest,
            "profile": profile.model_dump(mode="json"),
        }
    )
    return preview.model_copy(update={"digest": digest})


def resolve_agent_backend(
    channel: ConfigRevision | None, agent: AgentBackendRevision | None, *, enabled: bool = True
) -> ResolvedBackend:
    if not enabled:
        return resolve_default(None)
    if channel is not None:
        return ResolvedBackend.model_validate(
            channel.model_dump(include=set(ResolvedBackend.model_fields))
        )
    if agent is not None and agent.backend is not None:
        return backend_selection(agent.backend)
    return resolve_default(None)


def backend_selection_enabled(settings: TurnSettings) -> bool:
    return settings.path == "mux" and settings.channel_backends


class AgentBackendService:
    """Agent preferences build through channel admission, policy and audit owners."""

    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        *,
        enabled: bool = False,
        cost_line: Callable[[BackendAgent, Provider], str] | None = None,
    ) -> None:
        self._sessions = sessionmaker
        self.enabled = enabled
        self._cost_line: Callable[[BackendAgent, Provider], str] = cost_line or _default_cost

    def _require_enabled(self) -> None:
        if not self.enabled:
            raise AgentBackendRefused("feature_disabled")

    def _check_actor(self, tenant_id: uuid.UUID, actor: BackendActor) -> None:
        self._require_enabled()
        if actor.tenant_id != tenant_id:
            raise AgentBackendRefused("tenant_mismatch")
        if not authorize(OPEN_ACCESS_POLICY, subject=actor.subject, action=Action.SET_AGENT_RULE):
            raise AgentBackendRefused("admin_required")

    async def _actor(
        self, session: AsyncSession, tenant_id: uuid.UUID, actor: BackendActor
    ) -> None:
        self._check_actor(tenant_id, actor)
        policy = await policy_store.load_access_policy(session, tenant_id=tenant_id)
        if not authorize(policy, subject=actor.subject, action=Action.SET_AGENT_RULE):
            raise AgentBackendRefused("admin_required")
        if not await agent_backends.actor_exists(session, tenant_id, actor.account_id):
            raise AgentBackendRefused("actor_not_found")

    async def _policy(self, session: AsyncSession, tenant_id: uuid.UUID) -> TenantBackendPolicy:
        policy = await policy_store.load_access_policy(session, tenant_id=tenant_id)
        return TenantBackendPolicy(
            tenant_id=tenant_id,
            revision=_digest(policy.model_dump(mode="json")),
            enabled_backends=policy.enabled_backends,
        )

    async def tenant_policy(self, tenant_id: uuid.UUID) -> TenantBackendPolicy:
        async with self._sessions() as session:
            return await self._policy(session, tenant_id)

    async def current(self, agent: BackendAgent) -> AgentBackendRevision | None:
        if not self.enabled:
            return None
        async with self._sessions() as session:
            return await agent_backends.current(session, agent.tenant_id, agent.agent_id)

    async def revision(self, agent: BackendAgent, local: int) -> AgentBackendRevision | None:
        self._require_enabled()
        async with self._sessions() as session:
            return await agent_backends.current(
                session, agent.tenant_id, agent.agent_id, local=local
            )

    async def _preview(
        self, session: AsyncSession, agent: BackendAgent, target: Provider | None
    ) -> CompatibilityPreview:
        target = target or DEFAULT_BACKEND
        current = await agent_backends.current(session, agent.tenant_id, agent.agent_id)
        return compatibility_preview(
            agent,
            target,
            await self._policy(session, agent.tenant_id),
            setting_revision=current.local if current else 0,
            cost_line=self._cost_line(agent, target),
        )

    async def preview(
        self, agent: BackendAgent, target_backend: Provider | None = None
    ) -> CompatibilityPreview:
        self._require_enabled()
        async with self._sessions() as session:
            return await self._preview(session, agent, target_backend)

    async def read_selection(
        self,
        client: AsyncAnthropic,
        *,
        tenant_id: uuid.UUID,
        agent_name: str,
        target_backend: Provider | None = None,
        resource_host: NeutralResourceHost | None = None,
    ) -> BackendSelectionState | None:
        """Read an optional panel selection, preserving strict mutation refusals."""
        if not self.enabled:
            return None
        try:
            agent = await load_backend_agent(
                client,
                self._sessions,
                tenant_id=tenant_id,
                agent_name=agent_name,
                resource_host=resource_host,
            )
            return BackendSelectionState(
                agent=agent,
                revision=await self.current(agent),
                policy=await self.tenant_policy(tenant_id),
                preview=await self.preview(agent, target_backend),
            )
        except AgentBackendRefused:
            return None

    async def _change(
        self,
        agent: BackendAgent,
        backend: Provider | None,
        expected_revision: int,
        actor: BackendActor,
        operation: Literal["set", "clear", "revert", "fleet_grant_set", "fleet_grant_revoke"],
        expected_preview_digest: str | None,
        *,
        fleet_grant: AgentFleetGrant | None = None,
        now: datetime | None = None,
    ) -> AgentBackendRevision:
        self._check_actor(agent.tenant_id, actor)
        async with policy_store.policy_write_transaction(
            self._sessions, tenant_id=agent.tenant_id
        ) as session:
            await self._actor(session, agent.tenant_id, actor)
            current = await agent_backends.current(session, agent.tenant_id, agent.agent_id)
            local = current.local if current else 0
            if local != expected_revision:
                raise AgentBackendRefused("stale_revision")
            if operation == "revert":
                if current is None:
                    raise AgentBackendRefused("no_previous_revision")
                previous = (
                    await agent_backends.current(
                        session, agent.tenant_id, agent.agent_id, local=current.previous_local
                    )
                    if current.previous_local
                    else None
                )
                if current.previous_local is not None and previous is None:
                    raise AgentBackendRefused("no_previous_revision")
                backend = previous.backend if previous else None
            grant_operation = operation in ("fleet_grant_set", "fleet_grant_revoke")
            if grant_operation:
                if now is None:
                    raise AgentBackendRefused("invalid_fleet_grant")
                preview = await self._fleet_preview(session, agent, fleet_grant, now=now)
                backend = current.backend if current else None
                previous_local = current.previous_local if current else None
                retained_grant = preview.fleet_grant
            else:
                preview = await self._preview(session, agent, backend)
                if not preview.allowed:
                    raise AgentBackendRefused("backend_not_allowed")
                previous_local = local or None
                retained_grant = current.fleet_grant if current else None
            if (grant_operation or expected_preview_digest is not None) and (
                preview.digest != expected_preview_digest
            ):
                raise AgentBackendRefused("stale_preview")
            if not grant_operation:
                # Validation is the same owner used for explicit channel selections.
                check_backend(
                    ConfigRevision.create(
                        ChannelRef(
                            tenant_id=str(agent.tenant_id), platform="cli", channel_id=agent.name
                        ),
                        1,
                        backend_selection(preview.target_backend),
                    )
                )
            try:
                revision = await agent_backends.put_revision(
                    session,
                    tenant_id=agent.tenant_id,
                    agent_id=agent.agent_id,
                    agent_name=agent.name,
                    local=local + 1,
                    backend=backend,
                    previous_local=previous_local,
                    digest=_revision_digest(agent, backend, retained_grant),
                    preview_digest=preview.digest,
                    fleet_grant=retained_grant,
                )
            except IntegrityError:
                raise AgentBackendRefused("stale_revision") from None
            await security_audit.append_event(
                session,
                tenant_id=agent.tenant_id,
                account_id=actor.account_id,
                agent_id=agent.agent_id,
                platform=None,
                platform_user_id=None,
                tool_name="agent_backend",
                operation=operation,
                outcome="allowed",
                reason="completed",
                scope=f"agent_backend:{agent.agent_id}:{revision.local}",
            )
            return revision

    async def read_fleet_grant(
        self, tenant_id: uuid.UUID, agent_id: uuid.UUID, *, now: datetime
    ) -> AgentFleetGrant | None:
        if not self.enabled:
            return None
        if now.utcoffset() is None:
            raise AgentBackendRefused("invalid_fleet_grant")
        if not isinstance(cast(object, tenant_id), uuid.UUID) or not isinstance(
            cast(object, agent_id), uuid.UUID
        ):
            raise AgentBackendRefused("agent_identity_required")
        async with self._sessions() as session:
            current = await agent_backends.current(session, tenant_id, agent_id)
        if current is None:
            return None
        if current.fleet_grant_invalid:
            raise AgentBackendRefused("invalid_fleet_grant")
        if current.fleet_grant is None:
            return None
        value = AgentFleetGrant.model_validate_json(json.dumps(current.fleet_grant))
        return value if value.expires_at > now else None

    async def _fleet_preview(
        self,
        session: AsyncSession,
        agent: BackendAgent,
        value: AgentFleetGrant | None,
        *,
        now: datetime,
    ) -> CompatibilityPreview:
        current = await agent_backends.current(session, agent.tenant_id, agent.agent_id)
        local = current.local if current else 0
        policy = await self._policy(session, agent.tenant_id)
        normalized: JsonValue | None = None
        if value is not None:
            try:
                value = AgentFleetGrant.model_validate_json(value.model_dump_json())
                if now.utcoffset() is None or value.expires_at <= now:
                    raise ValueError("expired grant")
                for choice in value.backends:
                    check_backend(
                        ConfigRevision.create(
                            ChannelRef(
                                tenant_id=str(agent.tenant_id),
                                platform="cli",
                                channel_id=agent.name,
                            ),
                            1,
                            choice,
                        ),
                        enabled_backends=policy.enabled_backends,
                    )
                    value.limits.require_profile(get_profile(choice.profile))
            except (ValueError, DaimonError, UnsupportedCapability):
                raise AgentBackendRefused("invalid_fleet_grant") from None
            normalized = value.model_copy(update={"epoch": local + 1}).model_dump(mode="json")
        preview = compatibility_preview(
            agent,
            (current.backend if current else None) or DEFAULT_BACKEND,
            policy,
            setting_revision=local,
            cost_line="Fleet grant is an authority ceiling; funding is checked separately.",
        ).model_copy(update={"fleet_grant": normalized})
        return preview.model_copy(
            update={
                "digest": _digest(
                    {
                        **preview.model_dump(mode="json"),
                        "configuration_digest": agent.configuration_digest,
                    }
                )
            }
        )

    async def preview_fleet_grant(
        self, agent: BackendAgent, value: AgentFleetGrant | None, *, now: datetime
    ) -> CompatibilityPreview:
        self._require_enabled()
        async with self._sessions() as session:
            return await self._fleet_preview(session, agent, value, now=now)

    async def set_fleet_grant(
        self,
        agent: BackendAgent,
        value: AgentFleetGrant,
        expected_revision: int,
        actor: BackendActor,
        *,
        expected_preview_digest: str,
        now: datetime,
    ) -> AgentBackendRevision:
        return await self._change(
            agent,
            None,
            expected_revision,
            actor,
            "fleet_grant_set",
            expected_preview_digest,
            fleet_grant=value,
            now=now,
        )

    async def revoke_fleet_grant(
        self,
        agent: BackendAgent,
        expected_revision: int,
        actor: BackendActor,
        *,
        expected_preview_digest: str,
        now: datetime,
    ) -> AgentBackendRevision:
        return await self._change(
            agent,
            None,
            expected_revision,
            actor,
            "fleet_grant_revoke",
            expected_preview_digest,
            now=now,
        )

    async def set_backend(
        self,
        agent: BackendAgent,
        target_backend: Provider,
        expected_revision: int,
        actor: BackendActor,
        *,
        expected_preview_digest: str | None = None,
    ) -> AgentBackendRevision:
        return await self._change(
            agent, target_backend, expected_revision, actor, "set", expected_preview_digest
        )

    async def clear_backend(
        self,
        agent: BackendAgent,
        expected_revision: int,
        actor: BackendActor,
        *,
        expected_preview_digest: str | None = None,
    ) -> AgentBackendRevision:
        return await self._change(
            agent, None, expected_revision, actor, "clear", expected_preview_digest
        )

    async def revert(
        self,
        agent: BackendAgent,
        actor: BackendActor,
        *,
        expected_revision: int,
        expected_preview_digest: str | None = None,
    ) -> AgentBackendRevision:
        return await self._change(
            agent, None, expected_revision, actor, "revert", expected_preview_digest
        )

    async def audit(
        self,
        tenant_id: uuid.UUID,
        *,
        agent_id: uuid.UUID | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[security_audit.SecurityAuditRow, ...]:
        self._require_enabled()
        async with self._sessions() as session:
            rows = await security_audit.list_events(
                session,
                tenant_id=tenant_id,
                agent_id=agent_id,
                tool_name="agent_backend",
                limit=limit,
                offset=offset,
            )
        return tuple(rows)

    async def set_tenant_allowlist(
        self,
        tenant_id: uuid.UUID,
        backends: tuple[Provider, ...],
        expected_revision: str,
        actor: BackendActor,
        *,
        dry_run: bool = False,
    ) -> TenantBackendPolicy:
        self._check_actor(tenant_id, actor)
        async with policy_store.policy_write_transaction(
            self._sessions, tenant_id=tenant_id
        ) as session:
            await self._actor(session, tenant_id, actor)
            current = await self._policy(session, tenant_id)
            if current.revision != expected_revision:
                raise AgentBackendRefused("stale_revision")
            policy = await policy_store.load_access_policy(session, tenant_id=tenant_id)
            updated = type(policy).model_validate(
                {**policy.model_dump(), "enabled_backends": backends}
            )
            if dry_run:
                return current.model_copy(update={"enabled_backends": updated.enabled_backends})
            await policy_store.set_access_policy(session, tenant_id=tenant_id, policy=updated)
            await security_audit.append_event(
                session,
                tenant_id=tenant_id,
                account_id=actor.account_id,
                agent_id=None,
                platform=None,
                platform_user_id=None,
                tool_name="agent_backend",
                operation="allowlist",
                outcome="allowed",
                reason="completed",
            )
            return await self._policy(session, tenant_id)

    async def resolve(
        self, channel: ChannelRef, agent: BackendAgent, *, retained_revision: int | None = None
    ) -> BackendResolution:
        if not self.enabled:
            return BackendResolution("default", resolve_default(None))
        async with self._sessions.begin() as session:
            revision, _shared, source = await resolve_turn_backend(
                session,
                channel,
                agent,
                enabled=self.enabled,
                retained_revision=retained_revision,
            )
        return BackendResolution(source, resolve_agent_backend(revision, None), revision)


def make_backend_service(
    sessionmaker: async_sessionmaker[AsyncSession],
    settings: TurnSettings,
    *,
    cost_line: Callable[[BackendAgent, Provider], str] | None = None,
    prices: Mapping[str, ProviderPrice] | None = None,
) -> AgentBackendService:
    def configured_cost(agent: BackendAgent, target: Provider) -> str:
        return _default_cost(agent, target, prices=prices)

    return AgentBackendService(
        sessionmaker,
        enabled=backend_selection_enabled(settings),
        cost_line=cost_line or configured_cost,
    )


async def resolve_turn_backend(
    session: AsyncSession,
    channel: ChannelRef,
    agent: BackendAgent | None,
    *,
    enabled: bool,
    retained_revision: int | None = None,
    worker: ResolvedWorkerSpec | None = None,
    validate_origin: Callable[
        [ResolvedBackend, Literal["channel", "agent"], ConfigRevision | None],
        Awaitable[None] | None,
    ]
    | None = None,
) -> tuple[ConfigRevision | None, bool, Literal["channel", "agent", "default"]]:
    """Resolve and retain through the existing immutable channel revision namespace."""
    if not enabled or retained_revision == 0:
        return None, False, "default"
    if retained_revision is not None and worker is None:
        retained = await mux_state.get_config_revision(session, channel, retained_revision)
        origin = await mux_state.config_origin(session, channel, retained_revision)
        if retained is None or origin not in ("channel", "channel_clear", "agent"):
            raise AgentBackendRefused("stale_revision")
        source: Literal["channel", "agent"] = "agent" if origin == "agent" else "channel"
        return retained, retained.thread_mode == "shared", source
    if not isinstance(agent, BackendAgent):
        raise AgentBackendRefused("agent_identity_required")
    tenant_id = uuid.UUID(channel.tenant_id)
    if agent.tenant_id != tenant_id:
        raise AgentBackendRefused("tenant_mismatch")
    await policy_store.lock_policy_writes_shared(session, tenant_id=tenant_id)
    override, ever_shared = await current_backend_and_sharing(session, channel)
    if worker is None:
        if (
            override is not None
            and await mux_state.config_origin(session, channel, override.local) != "channel_clear"
        ):
            return override, ever_shared, "channel"
        setting = await agent_backends.current(session, tenant_id, agent.agent_id)
        if setting is None or setting.backend is None:
            return None, ever_shared, "default"
        selection = backend_selection(setting.backend)
    else:
        fleet = worker.permissions.fleet
        if (
            validate_origin is None
            or fleet.root.channel != channel
            or fleet.depth != 1
            or not fleet.enabled
            or not fleet.active
            or fleet.scope.is_platform
            or fleet.scope.is_legacy_host_authorized
            or fleet.scope.tenant_id != channel.tenant_id
            or fleet.scope.account_id != str(fleet.account_id)
            or worker.permissions.spec != worker.spec
            or worker.permissions.backend_grants != (worker.backend,)
            or worker.spec.model.provider != worker.backend.backend
            or worker.permissions.models != (worker.spec.model,)
            or (worker.backend.model is not None and worker.spec.model.id != worker.backend.model)
            or (
                worker.source_agent is None
                and worker.permissions.agent_id is not None
                and worker.permissions.agent_id != agent.agent_id
            )
        ):
            raise AgentBackendRefused("worker_identity_required")
        if worker.source_agent is not None and (
            agent.source is None or worker.source_agent != (agent.source.ref, agent.source.revision)
        ):
            raise AgentBackendRefused("stale_revision")
        selection = worker.backend
    policy = await policy_store.load_access_policy(session, tenant_id=tenant_id)
    if not backend_is_enabled(selection.backend, policy.enabled_backends):
        raise AgentBackendRefused("backend_not_allowed")
    if validate_origin is not None:
        checking = validate_origin(selection, "agent", None)
        if inspect.isawaitable(checking):
            await checking
    check_backend(ConfigRevision.create(channel, 1, selection))
    if retained_revision is not None:
        retained = await mux_state.get_config_revision(session, channel, retained_revision)
        if (
            retained is None
            or await mux_state.config_origin(session, channel, retained_revision) != "agent"
            or retained.content_digest() != selection.content_digest()
        ):
            raise AgentBackendRefused("stale_revision")
        if validate_origin is not None:
            checking = validate_origin(selection, "agent", retained)
            if inspect.isawaitable(checking):
                await checking
        return retained, ever_shared, "agent"
    for _ in range(3):
        effective = await mux_state.effective_revision(
            session, channel, selection.content_digest(), "agent"
        )
        if effective is not None:
            if validate_origin is not None:
                checking = validate_origin(selection, "agent", effective)
                if inspect.isawaitable(checking):
                    await checking
            return effective, ever_shared, "agent"
        effective = ConfigRevision.create(
            channel, await mux_state.next_config_local(session, channel), selection
        )
        if validate_origin is not None:
            checking = validate_origin(selection, "agent", effective)
            if inspect.isawaitable(checking):
                await checking
        try:
            await mux_state.put_config_revision(session, effective, origin="agent")
        except ConfigRevisionConflict:
            continue
        return effective, ever_shared, "agent"
    raise AgentBackendRefused("revision_busy")


async def load_backend_agent(
    anthropic: AsyncAnthropic,
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    agent_name: str,
    resource_host: NeutralResourceHost | None = None,
    actual_agent: BetaManagedAgentsAgent | None = None,
) -> BackendAgent:
    """Read actual tenant-scoped resources at the same edge used by skill edits."""
    if resource_host is not None:
        if resource_host.scope.tenant_id != str(tenant_id):
            raise AgentBackendRefused("tenant_mismatch")
        view = await resource_host.find(agent_name)
        if view is None:
            raise AgentBackendRefused("agent_not_found")
        row = await resource_host.read(view.revision.catalog_id)
        actual = row.agent
        skills, mcps = (tuple(items) for items in spec_compatibility_items(actual.spec))
        facts = actual.spec.model_dump(mode="json")
        return BackendAgent(
            tenant_id=tenant_id,
            agent_id=derive_agent_uuid(
                tenant_id=tenant_id,
                ma_agent_id="catalog:"
                + json.dumps(
                    [
                        resource_host.scope.account_id,
                        resource_host.backend.capabilities().provider,
                        resource_host.workspace,
                        row.catalog_id,
                    ],
                    separators=(",", ":"),
                ),
            ),
            name=agent_name,
            model=actual.spec.model.id,
            configuration_digest=_digest(
                {
                    "ref": actual.ref.model_dump(mode="json"),
                    "revision": actual.revision.model_dump(mode="json"),
                    "spec": facts,
                }
            ),
            skills=skills,
            mcps=mcps,
            required_capabilities=_native_agent_requirements(actual.spec),
            source=actual,
        )
    actual = actual_agent
    if actual is not None:
        metadata = actual.metadata or {}
        if metadata.get(MA_METADATA_KEY_TENANT) != str(tenant_id):
            raise AgentBackendRefused("tenant_mismatch")
        if metadata.get(MA_METADATA_KEY_NAME) != agent_name or actual.archived_at is not None:
            raise AgentBackendRefused("agent_not_found")
    else:
        actual = await find_agent_by_daimon_tag(anthropic, tenant_id=tenant_id, name=agent_name)
    if actual is None:
        raise AgentBackendRefused("agent_not_found")
    # Native SDK projection is hashed only; neither credentials nor URLs leave this edge.
    facts = actual.model_dump(mode="json")
    skills, mcps = (tuple(items) for items in spec_compatibility_items(actual))
    return BackendAgent(
        tenant_id=tenant_id,
        agent_id=derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=actual.id),
        name=agent_name,
        model=actual.model.id if not isinstance(actual.model, str) else actual.model,
        configuration_digest=_digest(facts),
        skills=skills,
        mcps=mcps,
        required_capabilities=("multiagent",) if actual.multiagent is not None else (),
        source=source_agent_record(actual, resource_scope(tenant_id=str(tenant_id))),
    )


def backend_is_enabled(backend: Provider, enabled_backends: tuple[Provider, ...]) -> bool:
    """One tenant-allowlist rule used by preview, writes and admission."""
    return backend in enabled_backends


def _default_cost(
    agent: BackendAgent, target: Provider, *, prices: Mapping[str, ProviderPrice] | None = None
) -> str:
    model = backend_selection(target).model or agent.model
    if model is None:
        return "List price unavailable: agent model unknown. Worst-case per turn: unknown."
    rates = (prices or {}).get(model) or MODEL_PRICING.get(model)
    return list_price_preview(model, rates)


def _native_agent_requirements(spec: AgentSpec) -> tuple[Capability, ...]:
    required: list[Capability] = []
    if any(tool.permission == "ask" for tool in spec.tools or ()):
        required.append("tool_confirmation")
    if "anthropic.multiagent" in (spec.extensions or {}):
        required.append("multiagent")
    return tuple(required)


async def current_agent_backend(
    session: AsyncSession, tenant_id: uuid.UUID, agent_id: uuid.UUID
) -> AgentBackendRevision | None:
    """Read the current preference for a host-resolved scoped resource identity."""
    if not isinstance(cast(object, agent_id), uuid.UUID):
        raise AgentBackendRefused("agent_identity_required")
    return await agent_backends.current(session, tenant_id, agent_id)
