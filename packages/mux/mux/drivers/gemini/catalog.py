"""Default-off native compiler/readback edge for the host's scoped CAS catalog.

source-G-agent.txt:925-1069 documents agents.create/get and no versioning.
Host metadata and skill version manifests remain authored intent; they are
not fictional provider metadata or provider version numbers.
"""

from copy import deepcopy

from mux.contracts.ids import Page, PageRequest, ResourceRef, Revision, Scope
from mux.contracts.resources import Agent, AgentFilter, AgentSpec
from mux.drivers.gemini.bundles import owned_skill, skill_sources, validate_bundle, validate_targets
from mux.drivers.gemini.catalog_transport import AgentCatalogTransport
from mux.drivers.gemini.core import GeminiAgents, compile_agent, now, page_values, refuse
from mux.drivers.gemini.mcp_auth import MCPSecretResolver
from mux.drivers.gemini.skill_deployment import GCSBundleDeployment
from mux.drivers.gemini.storage import Records, Storage, owner
from mux.drivers.gemini.transport import Object, Transport, object_value
from mux.errors import ProviderError, ScopeViolation
from mux.profiles.gemini import INLINE_REUSE
from mux.state.operations import request_digest

MODE = "gemini.named_agent_catalog@1"


def require_scope(scope: Scope) -> None:
    if (
        scope.is_platform
        or scope.is_legacy_host_authorized
        or not scope.principal_id.strip()
        or not scope.authorization_id.strip()
    ):
        raise ScopeViolation("gemini-resource-catalog", "explicit scoped authorization required")


def native_definition(
    records: Records,
    scope: Scope,
    account: str,
    spec: AgentSpec,
    id_: str,
    *,
    authenticated_mcp: bool = False,
) -> Object:
    """Keep an explicit native model/tools list and every concrete bundle byte."""
    # Persistent native definitions cannot safely store per-session credentials.
    if spec.model.id not in (
        "gemini-3.8-flash",
        "gemini-3.7-flash",
        "gemini-3.6-flash",
        "gemini-3.5-flash",
        "gemini-3.5-flash-lite",
    ):
        refuse("native_catalog_model")
    if not authenticated_mcp and any(
        connection.credential_ref is not None for connection in spec.mcp_servers or ()
    ):
        refuse("named_agent_session_credentials")
    # Compilation contains destination/allowlist only. The existing session
    # resolver attaches bearer material to each interaction, never this object.
    request = compile_agent(spec, authenticated_mcp=authenticated_mcp)
    for pin in spec.skills or ():
        record = owned_skill(records, scope, account, pin.id)
        if pin.version in (None, "latest") or (
            pin.digest is not None and pin.digest != pin.version
        ):
            refuse("concrete_catalog_skill_pin")
        bundle = record.bundles.get(pin.version)
        if (
            bundle is None
            or validate_bundle(bundle, external=pin.version in record.deployments) != pin.version
        ):
            raise ProviderError("conflict", retryable=False, native_code="catalog_skill_drift")
    sources = skill_sources(records, scope, account, spec.skills)
    validate_targets(sources)
    definition: Object = {
        "id": id_,
        "base_agent": request["agent"],
        "agent_config": request["agent_config"],
        "tools": request["tools"],
        "base_environment": {"type": "remote", "sources": sources},
    }
    if spec.description is not None:
        definition["description"] = spec.description
    if spec.system is not None:
        definition["system_instruction"] = spec.system
    return definition


def verify_definition(observed: Object, expected: Object) -> None:
    """Allow proto3 omission of empty lists, retaining all other definition proof."""
    for field in ("id", "base_agent", "agent_config", "tools", "base_environment"):
        actual = observed.get(field)
        wanted = expected[field]
        # Repeated fields may be omitted on readback only when compiled empty.
        # Copy nested data so verification never alters the provider evidence.
        if field == "tools" and field not in observed and wanted == []:
            continue
        if (
            field == "base_environment"
            and isinstance(actual, dict)
            and isinstance(wanted, dict)
            and wanted.get("sources") == []
            and "sources" not in actual
        ):
            actual = {**actual, "sources": []}
        # The documented remote shorthand is equivalent only without mounts.
        if (
            field == "base_environment"
            and wanted == {"type": "remote", "sources": []}
            and actual == "remote"
        ):
            continue
        if actual != wanted:
            raise ProviderError(
                "conflict", retryable=False, native_code="catalog_native_definition_drift"
            )
    for field in ("description", "system_instruction"):
        if observed.get(field) != expected.get(field):
            raise ProviderError(
                "conflict", retryable=False, native_code="catalog_native_definition_drift"
            )


class GeminiCatalogAdapter(GeminiAgents):
    """Native resource ports plus independent readback for NeutralResourceHost.

    The host retains authored rows and does CAS through PostgresNeutralCatalog.
    A pending creation is fenced before POST and can only reconcile through GET
    after restart. No accepted/ambiguous POST is retried or turned into a local
    native agent. Default inline agent ports are not changed by this adapter.
    """

    provider = "gemini"
    profile_id = INLINE_REUSE.profile_id

    def __init__(
        self,
        transport: Transport,
        storage: Storage,
        account_scope_id: str,
        catalog_transport: AgentCatalogTransport,
        *,
        enabled: bool = False,
        mcp_secrets: MCPSecretResolver | None = None,
    ) -> None:
        if type(enabled) is not bool or not account_scope_id.strip():
            raise ValueError("an explicit bool capability and account scope are required")
        super().__init__(transport, storage, account_scope_id, mcp_secrets=mcp_secrets)
        self.enabled = enabled
        self._catalog_transport = catalog_transport

    @property
    def account_scope_id(self) -> str:
        return self._account

    def _admit_catalog(self, scope: Scope) -> None:
        if not self.enabled:
            refuse("gemini_resource_catalog")
        require_scope(scope)

    async def compile(self, scope: Scope, spec: AgentSpec, *, id_: str) -> Object:
        self._admit_catalog(scope)
        async with self._storage.transaction() as records:
            return native_definition(
                records,
                scope,
                self._account,
                spec,
                id_,
                authenticated_mcp=self._mcp_secrets is not None,
            )

    async def create(self, scope: Scope, spec: AgentSpec, *, key: str) -> Agent:
        self._admit_catalog(scope)
        if not key.strip():
            raise ValueError("a durable operation key is required")
        # Stable identity is bound to the complete host routing/principal tuple.
        # It is independent of content, so a changed request cannot mint a second
        # native object after an ambiguous first request with the same key.
        id_ = (
            "daimon-"
            + request_digest(
                {
                    "tenant": scope.tenant_id,
                    "account": scope.account_id,
                    "principal": scope.principal_id,
                    "workspace": self._account,
                    "key": key,
                }
            )[:40]
        )
        async with self._storage.transaction() as records:
            definition = native_definition(
                records,
                scope,
                self._account,
                spec,
                id_,
                authenticated_mcp=self._mcp_secrets is not None,
            )
            digest = request_digest(
                {"kind": MODE, "spec": spec.model_dump(mode="json"), "definition": definition}
            )
            prior = self._creation(records, scope, key, digest)
            if prior is not None and prior != id_:
                raise ProviderError(
                    "conflict", retryable=False, native_code="catalog_identity_drift"
                )
            if prior is None:
                # Commit intent separately, before any provider side effect.
                records.creations[(*owner(scope), key)] = scope.principal_id, digest, id_
        if prior is None:
            acknowledgement = await self._catalog_transport.create_agent(definition)
            if acknowledgement.get("id") != id_:
                raise ProviderError(
                    "upstream", retryable=False, native_code="catalog_create_unverified"
                )
        try:
            observed = await self._catalog_transport.get_agent(id_)
        except ProviderError as error:
            if error.category == "not_found":
                raise ProviderError(
                    "upstream", retryable=False, native_code="catalog_create_unverified"
                ) from None
            raise
        verify_definition(observed, definition)
        async with self._storage.transaction() as records:
            self._creation(records, scope, key, digest)
            current = records.agents.get(id_)
            if current is not None:
                retained = self._owned(current, scope)
                if current.spec != spec or retained != definition:
                    raise ProviderError(
                        "conflict", retryable=False, native_code="catalog_agent_drift"
                    )
                return current
            agent = Agent(
                ref=self._ref(scope, id_, "agent"),
                revision=Revision(local=1),
                spec=spec,
                created_at=now(),
                native={"resource_mode": MODE, "definition": deepcopy(definition)},
            )
            records.agents[id_] = agent
            return agent

    def _owned(self, agent: Agent, scope: Scope) -> Object:
        self._check(scope, agent.ref, "agent")
        raw = agent.native
        if not isinstance(raw, dict) or raw.get("resource_mode") != MODE:
            raise ScopeViolation(agent.ref.id, "not an owned native catalog agent")
        definition = raw.get("definition")
        if not isinstance(definition, dict) or definition.get("id") != agent.ref.id:
            raise ProviderError("conflict", retryable=False, native_code="catalog_identity_drift")
        return object_value(definition)

    async def retrieve(self, scope: Scope, ref: ResourceRef) -> Agent:
        self._admit_catalog(scope)
        self._check(scope, ref, "agent")
        async with self._storage.transaction() as records:
            agent = records.agents.get(ref.id)
            if agent is None or agent.ref != ref:
                raise ScopeViolation(ref.id, "no owned native catalog agent")
            expected = deepcopy(self._owned(agent, scope))
            compiled = native_definition(
                records,
                scope,
                self._account,
                agent.spec,
                ref.id,
                authenticated_mcp=self._mcp_secrets is not None,
            )
            if expected != compiled:
                raise ProviderError(
                    "conflict", retryable=False, native_code="catalog_compiler_drift"
                )
        verify_definition(await self._catalog_transport.get_agent(ref.id), expected)
        return agent

    async def skill_deployments(
        self, scope: Scope, retained: Agent
    ) -> tuple[GCSBundleDeployment, ...]:
        """Read the owned deployments actually mounted by this verified agent.

        The host's storage readback must check these directories, rather than
        accepting an unrelated operator receipt with the same version label.
        """
        self._admit_catalog(scope)
        self._owned(retained, scope)
        async with self._storage.transaction() as records:
            if records.agents.get(retained.ref.id) != retained:
                raise ScopeViolation(retained.ref.id, "skill deployment agent differs")
            skill_sources(records, scope, self._account, retained.spec.skills)
            deployments: list[GCSBundleDeployment] = []
            for pin in retained.spec.skills or ():
                record = owned_skill(records, scope, self._account, pin.id)
                deployment = record.deployments.get(pin.version or "")
                if deployment is None:
                    raise ProviderError(
                        "conflict", retryable=False, native_code="skill_deployment_missing"
                    )
                deployments.append(deployment)
            return tuple(deployments)

    async def verify(self, scope: Scope, retained: Agent) -> Agent:
        """The host edge compares its separate authored row with native readback."""
        fresh = await self.retrieve(scope, retained.ref)
        if fresh.revision != retained.revision or fresh.spec != retained.spec:
            raise ProviderError("conflict", retryable=False, native_code="catalog_authored_drift")
        return fresh

    async def list(self, scope: Scope, *, filters: AgentFilter, page: PageRequest) -> Page[Agent]:
        self._admit_catalog(scope)
        async with self._storage.transaction() as records:
            candidates = tuple(
                agent
                for agent in records.agents.values()
                if agent.ref == self._ref(scope, agent.ref.id, "agent")
                and isinstance(agent.native, dict)
                and agent.native.get("resource_mode") == MODE
                and (filters.name is None or agent.spec.name == filters.name)
                and (filters.include_archived or agent.lifecycle == "active")
                and (filters.created_after is None or agent.created_at > filters.created_after)
            )
        selected = page_values(candidates, page)
        return selected.model_copy(
            update={
                "data": tuple([await self.retrieve(scope, agent.ref) for agent in selected.data])
            }
        )
