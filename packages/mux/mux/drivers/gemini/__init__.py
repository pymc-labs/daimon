"""Explicitly constructed Gemini inline-reuse driver; never a resolved default."""

from collections.abc import Callable

from mux.contracts.admission import Admission, admit
from mux.contracts.config import ConfigRevision
from mux.contracts.ids import ModelRef, Page, PageRequest, ResourceRef, Scope
from mux.contracts.ports import Models
from mux.contracts.profile import Profile
from mux.contracts.resources import Agent
from mux.contracts.usage import UsageObservation
from mux.drivers.gemini.catalog import GeminiCatalogAdapter
from mux.drivers.gemini.catalog_transport import AgentCatalogTransport
from mux.drivers.gemini.core import (
    Base,
    GeminiAgents,
    GeminiEnvironments,
    GeminiEvents,
    GeminiSessions,
    compile_agent,
    compile_environment,
    page_values,
)
from mux.drivers.gemini.deployment import (
    configured_catalog_transport,
    configured_transport,
    downscoped_storage_token,
)
from mux.drivers.gemini.flash import (
    ATTEMPT_MODELS,
    FLASH_MODELS,
    FLASH_PRIMARY,
    FlashAccounting,
    FlashAttempt,
    FlashTransport,
)
from mux.drivers.gemini.mcp_auth import GCSSecretResolver, MCPSecretResolver
from mux.drivers.gemini.resources import GeminiArtifacts, GeminiSkills
from mux.drivers.gemini.skill_deployment import (
    GCSBundleDeployment,
    RetainedSkillDeployments,
    SkillBundlePublisher,
    verify_retained_deployments,
)
from mux.drivers.gemini.storage import Records, Storage, read_records
from mux.drivers.gemini.transport import NO_TOOLS_FUNCTION, NO_TOOLS_TOOL, SDKTransport, Transport
from mux.errors import UnsupportedCapability
from mux.profiles.gemini import INLINE_REUSE as PROFILE
from mux.state.store import StateStore


class GeminiUsage(Base):
    async def reconcile(self, scope: Scope, session: ResourceRef) -> tuple[UsageObservation, ...]:
        return await self.retrying(lambda: self._reconcile(scope, session))

    async def _reconcile(self, scope: Scope, session: ResourceRef) -> tuple[UsageObservation, ...]:
        # Provider reads first, holding no lock; then a short write to apply them.
        fetched = await self._fetch_interactions(scope, session)
        async with self._storage.transaction() as records:
            record = self._record(records, scope, session)
            self._apply_fetched(record, fetched)
            return tuple(record.observation_history.values())

    async def list(
        self, scope: Scope, session: ResourceRef, *, page: PageRequest
    ) -> Page[UsageObservation]:
        # A committed snapshot: listing usage never waits behind the account's writers.
        async with read_records(self._storage) as records:
            record = self._record(records, scope, session)
            return page_values(tuple(record.observation_history.values()), page)


class GeminiManagedAgents:
    """Host injects transactional storage, StateStore and a private transport.

    SDKTransport is an optional edge; offline construction needs no SDK client,
    credentials or discovery. No persistence implementation is silently selected.
    Host admission, thread lease and journal/accounting wiring are separate seams.
    """

    def __init__(
        self,
        transport: Transport,
        *,
        storage: Storage,
        state_store: StateStore,
        account_scope_id: str,
        model_for_interaction: Callable[[str], ModelRef | None] | None = None,
        mcp_secrets: MCPSecretResolver | None = None,
        gcs_secrets: GCSSecretResolver | None = None,
        skill_bundle_publisher: SkillBundlePublisher | None = None,
        resource_catalog_enabled: bool = False,
        resource_catalog_transport: AgentCatalogTransport | None = None,
        native_agent: Agent | None = None,
        max_total_tokens: int | None = None,
        max_output_tokens: int | None = None,
        max_input_tokens: int | None = None,
    ) -> None:
        if type(resource_catalog_enabled) is not bool:
            raise ValueError("resource catalog capability must be an explicit bool")
        if max_output_tokens is not None and (
            ((native_agent is not None or resource_catalog_enabled) and max_input_tokens is None)
            or max_total_tokens is not None
        ):
            raise UnsupportedCapability(("model_interaction_resources",), PROFILE.profile_id)
        self.catalog_adapter: GeminiCatalogAdapter | None = None
        if resource_catalog_enabled:
            if resource_catalog_transport is None:
                raise UnsupportedCapability(
                    ("gemini_resource_catalog_transport",), PROFILE.profile_id
                )
            self.catalog_adapter = GeminiCatalogAdapter(
                transport,
                storage,
                account_scope_id,
                resource_catalog_transport,
                enabled=True,
                mcp_secrets=mcp_secrets,
            )
        self.agents = self.catalog_adapter or GeminiAgents(
            transport, storage, account_scope_id, mcp_secrets=mcp_secrets
        )
        self.environments = GeminiEnvironments(transport, storage, account_scope_id)
        self.sessions = GeminiSessions(
            transport,
            storage,
            account_scope_id,
            mcp_secrets=mcp_secrets,
            native_agent=native_agent,
            gcs_secrets=gcs_secrets,
            max_total_tokens=max_total_tokens,
            max_output_tokens=max_output_tokens,
            max_input_tokens=max_input_tokens,
        )
        self.events = GeminiEvents(
            transport,
            storage,
            account_scope_id,
            state_store=state_store,
            model_for_interaction=model_for_interaction,
            native_agent=native_agent,
            gcs_secrets=gcs_secrets,
            mcp_secrets=mcp_secrets,
            max_total_tokens=max_total_tokens,
            max_output_tokens=max_output_tokens,
            max_input_tokens=max_input_tokens,
        )
        self.usage = GeminiUsage(transport, storage, account_scope_id)
        self.artifacts = GeminiArtifacts(transport, storage, account_scope_id)
        self.skills = GeminiSkills(
            transport, storage, account_scope_id, publisher=skill_bundle_publisher
        )

    @property
    def models(self) -> Models:
        raise UnsupportedCapability(("model_discovery",), PROFILE.profile_id)

    def capabilities(self) -> Profile:
        return PROFILE

    def admit(self, config: ConfigRevision) -> Admission:
        return admit(config, self.capabilities())

    def extension[T](self, port: type[T], *, namespace: str, version: int) -> T:
        self.capabilities().offered_extension(namespace, version)
        # gemini.session is a closed configuration schema, not an execution port.
        raise UnsupportedCapability((namespace,), PROFILE.profile_id)


__all__ = [
    "NO_TOOLS_FUNCTION",
    "NO_TOOLS_TOOL",
    "ATTEMPT_MODELS",
    "AgentCatalogTransport",
    "FLASH_MODELS",
    "FLASH_PRIMARY",
    "FlashAccounting",
    "FlashAttempt",
    "FlashTransport",
    "GCSBundleDeployment",
    "GCSSecretResolver",
    "GeminiAgents",
    "GeminiArtifacts",
    "GeminiCatalogAdapter",
    "GeminiEnvironments",
    "GeminiEvents",
    "GeminiManagedAgents",
    "GeminiSessions",
    "GeminiSkills",
    "GeminiUsage",
    "MCPSecretResolver",
    "Records",
    "RetainedSkillDeployments",
    "SkillBundlePublisher",
    "Storage",
    "SDKTransport",
    "Transport",
    "compile_agent",
    "compile_environment",
    "configured_catalog_transport",
    "configured_transport",
    "downscoped_storage_token",
    "verify_retained_deployments",
]
