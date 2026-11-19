"""Per-admitted-channel runtime construction, without startup credential access."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable, Iterator, Mapping
from dataclasses import dataclass, replace
from importlib import import_module
from types import MappingProxyType
from typing import TYPE_CHECKING, Protocol, cast

import structlog
from daimon.core.mux_backend import TurnRuntime
from daimon.core.turn.errors import AdmissionDenied
from mux.errors import ScopeViolation, UnsupportedCapability
from mux.profiles import get_profile

if TYPE_CHECKING:
    from daimon.core.auxiliary_calls import CompletionsFactory
    from daimon.core.mux_backend import BackendFactory
    from daimon.core.pricing import ProviderPrice
    from daimon.core.turn.carry_over import SessionReader
    from daimon.core.turn.io import TurnCodecFactory
    from daimon.core.turn.prepare import ProviderPreparation, ProviderPreparationRequest
    from mux.contracts.ids import Provider

RuntimeFactory = Callable[["ProviderPreparationRequest"], TurnRuntime]
# Async provider setup a turn needs before its (sync) runtime is built, e.g.
# creating a tenant's hosted environment once. Runs only on a real turn's preparation.
RuntimePreparation = Callable[
    ["ProviderPreparationRequest"], Awaitable["ProviderPreparationRequest"]
]


class RuntimeDeployment(Protocol):
    """Read-only operator fields consumed by resource authorization callers."""

    @property
    def account_id(self) -> str: ...

    @property
    def platform(self) -> str: ...

    @property
    def channel_id(self) -> str: ...

    @property
    def config_digest(self) -> str | None: ...

    @property
    def project(self) -> str: ...


DeploymentLoader = Callable[[str], tuple[RuntimeDeployment, ...]]


class AgentGrant(Protocol):
    """An operator's opt-in for one agent's own configuration on a profile."""

    @property
    def source_agent_id(self) -> uuid.UUID | None: ...

    @property
    def config_digest(self) -> str | None: ...

    @property
    def environment_id(self) -> str: ...


@dataclass(frozen=True)
class HostedBackend:
    """Everything the host needs from one non-Anthropic profile; a missing hook fails closed."""

    runtime: RuntimeFactory | None = None
    backend: BackendFactory | None = None
    codec: TurnCodecFactory | None = None
    prepare: ProviderPreparation | None = None
    reader: SessionReader | None = None
    authorizations: DeploymentLoader | None = None
    tariffs: Callable[[], Mapping[tuple[str, str], ProviderPrice]] | None = None
    prepare_runtime: RuntimePreparation | None = None
    # Set by a profile an agent may select only where the operator granted it.
    agent_grants: Callable[[str], tuple[AgentGrant, ...]] | None = None
    # Its host runs an agent's own copy in a shared thread as the thread's owner.
    shares_agent_threads: bool = False
    # Side calls for installs without an Anthropic key, offered while its key is set.
    completions: CompletionsFactory | None = None


#: Each hosted profile's module exports `BACKEND`; Anthropic is the default path.
_MODULES: Mapping[str, str] = MappingProxyType(
    {
        "openai.persistent_workspace": "daimon.core.turn.openai_deployment",
        "gemini.inline_reuse": "daimon.core.turn.gemini_deployment",
        "selfhosted.loop": "daimon.core.turn.selfhosted_deployment",
    }
)
#: Imported on first use. Tests replace an entry to inject a fake backend.
_BACKENDS: dict[str, HostedBackend] = {}


def hosted_backend(profile: str) -> HostedBackend | None:
    """The profile's host callbacks, importing its module without clients or credentials."""
    backend = _BACKENDS.get(profile)
    if backend is None and profile in _MODULES:
        try:
            backend = cast(HostedBackend, import_module(_MODULES[profile]).BACKEND)
        except ImportError:
            return None
        _BACKENDS[profile] = backend
    return backend


def hosted_profiles() -> tuple[str, ...]:
    """Every hosted profile, loaded, plus any a test injected."""
    return tuple(dict.fromkeys((*_MODULES, *_BACKENDS)))


def provider_completions(provider: Provider) -> CompletionsFactory | None:
    """A keyed provider's side-call driver, without building it."""
    from daimon.core.provider_keys import provider_key_present

    for profile in hosted_profiles():
        backend = hosted_backend(profile)
        if (
            backend is not None
            and backend.completions is not None
            and get_profile(profile).provider == provider
            and provider_key_present(provider)
        ):
            return backend.completions
    return None


def shares_agent_threads(profile: str) -> bool:
    """Whether `profile` runs an agent's own copy in a shared thread (as its owner)."""
    backend = hosted_backend(profile)
    return backend is not None and backend.shares_agent_threads


async def preload_channel_runtimes() -> None:
    """Install every provider runtime off the event loop before a process serves turns.

    Otherwise a provider's first turn imports its SDK on the loop, stalling every turn
    in the process for seconds.
    """

    def install() -> None:
        for profile in hosted_profiles():
            try:
                hosted_backend(profile)
            except Exception:  # one broken provider must not stop the process
                structlog.get_logger().warning(
                    "runtime.preload_failed", profile=profile, exc_info=True
                )

    await asyncio.to_thread(install)


def authorized_channel_deployments(profile: str, tenant_id: str) -> tuple[RuntimeDeployment, ...]:
    """Re-read provider-validated operator grants, without a turn or credential lookup."""
    backend = hosted_backend(profile)
    if backend is None or backend.authorizations is None:
        raise UnsupportedCapability(("host_deployment_authorization",), profile)
    return backend.authorizations(tenant_id)


def granted_agent_environments(
    profile: str, tenant_id: str, agent_id: uuid.UUID, config_digest: str
) -> tuple[str, ...] | None:
    """The environments an operator granted this agent's configuration on `profile`.

    None: the profile takes no per-agent grant. Empty: it needs one and none exists.
    """
    backend = hosted_backend(profile)
    if backend is None or backend.agent_grants is None:
        return None
    return tuple(
        dict.fromkeys(
            grant.environment_id
            for grant in backend.agent_grants(tenant_id)
            if grant.source_agent_id == agent_id and grant.config_digest == config_digest
        )
    )


def has_agent_grants(profile: str, tenant_id: str) -> bool:
    """Whether any agent of this tenant has an operator grant on `profile`."""
    backend = hosted_backend(profile)
    return (
        backend is not None
        and backend.agent_grants is not None
        and bool(backend.agent_grants(tenant_id))
    )


def agent_granted_prices(
    tenant_id: str, agent_id: uuid.UUID | None = None
) -> Mapping[tuple[str, str], ProviderPrice]:
    """Tariffs of the grant-gated profiles where this tenant has an agent grant.

    With `agent_id`, only profiles where that agent holds a grant.
    """
    prices: dict[tuple[str, str], ProviderPrice] = {}
    for profile in hosted_profiles():
        backend = hosted_backend(profile)
        if backend is None:
            continue
        read, tariffs = backend.agent_grants, backend.tariffs
        if (
            read is not None
            and tariffs is not None
            and any(agent_id in (None, grant.source_agent_id) for grant in read(tenant_id))
        ):
            prices.update(tariffs())
    return MappingProxyType(prices)


async def prepare_channel_runtime(
    request: ProviderPreparationRequest,
) -> ProviderPreparationRequest:
    """Run the admitted profile's async setup, if it has one, before its runtime is built.

    Injected runtimes (tests, explicit host overrides) skip it: they need no setup.
    """
    revision = request.admission.backend_revision
    runtimes = request.deps.turn_runtimes
    if revision is None:
        return request
    if isinstance(runtimes, ScopedTurnRuntimes):
        if runtimes.has_runtime(revision.profile):
            return request
    elif revision.profile in runtimes:
        return request
    backend = hosted_backend(revision.profile)
    if backend is None or backend.prepare_runtime is None:
        return request
    return await backend.prepare_runtime(request)


def build_channel_runtime(request: ProviderPreparationRequest) -> TurnRuntime:
    """Construct only the explicitly admitted provider's scoped runtime.

    Provider deployment constructors own native resource plans and private
    transport/credential lifetimes. Missing constructors fail closed, without
    inspecting another provider's configuration or credentials.
    """
    revision = request.admission.backend_revision
    if (
        revision is None
        or revision.profile == "anthropic.managed_agents"
        or not request.deps.channel_backends
    ):
        raise AdmissionDenied(reason="backend_unsupported")
    if (
        revision.channel.tenant_id != request.scope.tenant_id
        or revision.channel.platform != request.platform
        or str(request.tenant_id) != request.scope.tenant_id
        or str(request.session_account_id)
        != str(request.admission.shared_owner or request.scope.account_id)
        or request.scope.is_platform
        or request.scope.is_legacy_host_authorized
        or revision.backend != get_profile(revision.profile).provider
        or revision.model is None
    ):
        raise ScopeViolation(request.thread_id, "runtime differs from admitted channel scope")
    backend = hosted_backend(revision.profile)
    if backend is None or backend.runtime is None:
        raise AdmissionDenied(reason="backend_unsupported")
    return backend.runtime(request)


class ScopedTurnRuntimes(Mapping[str, TurnRuntime]):
    """A turn's lazy runtime lookup; never shared across channels or revisions.

    Explicit injected runtimes take precedence. Provider preparation chooses
    when to request the runtime, preserving its checks before transport use.
    The successful runtime is carried into execution rather than reconstructed.
    """

    def __init__(self, request: ProviderPreparationRequest, factory: RuntimeFactory) -> None:
        self._request = request
        self._factory = factory
        self._runtimes = dict(request.deps.turn_runtimes)
        revision = request.admission.backend_revision
        self._profile = revision.profile if revision is not None else None

    def bind_preparation(self, request: ProviderPreparationRequest) -> None:
        """Attach canonical preparation facts before the first scoped construction."""
        if (
            request.scope != self._request.scope
            or request.admission.backend_revision != self._request.admission.backend_revision
            or request.platform != self._request.platform
            or request.thread_id != self._request.thread_id
            or request.session_account_id != self._request.session_account_id
        ):
            raise ScopeViolation(request.thread_id, "preparation differs from runtime scope")
        if self.resolved is None:
            self._request = request

    def has_runtime(self, profile: str) -> bool:
        """Whether `profile` already has a runtime (injected or built), without building one."""
        return profile in self._runtimes

    @property
    def resolved(self) -> TurnRuntime | None:
        """Inspect the selected runtime without constructing it."""
        return self._runtimes.get(self._profile) if self._profile is not None else None

    def __getitem__(self, profile: str) -> TurnRuntime:
        runtime = self._runtimes.get(profile)
        if runtime is not None:
            return runtime
        if profile != self._profile:
            raise KeyError(profile)
        runtime = self._factory(self._request)
        self._runtimes[profile] = runtime
        return runtime

    def __iter__(self) -> Iterator[str]:
        return iter(dict.fromkeys((*self._runtimes, *((self._profile,) if self._profile else ()))))

    def __len__(self) -> int:
        return len(self._runtimes) + int(
            self._profile is not None and self._profile not in self._runtimes
        )


def scope_channel_runtimes(
    request: ProviderPreparationRequest,
) -> tuple[ProviderPreparationRequest, ScopedTurnRuntimes | None]:
    """Bind the existing lazy runtime owner to one admitted configuration and scope."""
    from daimon.core.stores.mux_state import PostgresStateStore

    deps = request.deps
    if not deps.channel_backends or deps.turn_runtime_factory is None:
        return request, None
    provider_deps = replace(
        deps, state_store=deps.state_store or PostgresStateStore(deps.sessionmaker)
    )
    request = replace(request, deps=provider_deps)
    runtimes = ScopedTurnRuntimes(request, deps.turn_runtime_factory)
    return replace(request, deps=replace(provider_deps, turn_runtimes=runtimes)), runtimes
