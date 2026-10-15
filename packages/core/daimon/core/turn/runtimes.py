"""Per-admitted-channel runtime construction, without startup credential access."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, replace
from importlib.metadata import entry_points
from types import MappingProxyType
from typing import TYPE_CHECKING, Protocol, cast

from daimon.core.mux_backend import TurnRuntime
from daimon.core.turn.errors import AdmissionDenied
from mux.errors import ScopeViolation, UnsupportedCapability
from mux.profiles import get_profile

if TYPE_CHECKING:
    from daimon.core.pricing import ProviderPrice
    from daimon.core.turn.prepare import ProviderPreparationRequest

RuntimeFactory = Callable[["ProviderPreparationRequest"], TurnRuntime]


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


@dataclass(frozen=True)
class _RuntimeRegistration:
    factory: RuntimeFactory
    authorizations: DeploymentLoader | None = None
    tariffs: Callable[[], Mapping[tuple[str, str], ProviderPrice]] | None = None


_RUNTIME_FACTORIES: dict[str, _RuntimeRegistration] = {}


def register_channel_runtime(
    profile: str,
    factory: RuntimeFactory,
    *,
    authorizations: DeploymentLoader | None = None,
    tariffs: Callable[[], Mapping[tuple[str, str], ProviderPrice]] | None = None,
    fallback: bool = False,
) -> None:
    """Register a constructor; provider defaults preserve explicit host overrides."""
    if profile == "anthropic.managed_agents":
        raise ValueError(f"channel runtime already registered: {profile}")
    if profile in _RUNTIME_FACTORIES:
        if fallback:
            return
        raise ValueError(f"channel runtime already registered: {profile}")
    _RUNTIME_FACTORIES[profile] = _RuntimeRegistration(factory, authorizations, tariffs)


def ensure_channel_runtime(profile: str, *, ready: Callable[[], bool] | None = None) -> bool:
    """Install only the selected profile through its provider-owned entry point.

    Existing constructors and readers take precedence, but a missing host edge
    still needs installation. The caller supplies its existing registry lookup
    when it needs such an edge. Default turns never discover distributions;
    registrars bind callbacks without clients, credentials or deployment reads.
    """
    if profile == "anthropic.managed_agents":
        return False
    if profile in _RUNTIME_FACTORIES and (ready is None or ready()):
        return True
    selected = entry_points(group="daimon.turn_runtimes", name=profile)
    if len(selected) != 1:
        return False
    try:
        register = cast(Callable[[], None], next(iter(selected)).load())
        register()
    except (ImportError, AttributeError, TypeError, ValueError):
        return False
    return profile in _RUNTIME_FACTORIES and (ready is None or ready())


def authorized_channel_deployments(profile: str, tenant_id: str) -> tuple[RuntimeDeployment, ...]:
    """Re-read provider-validated operator grants, without a turn or credential lookup."""
    if not ensure_channel_runtime(profile):
        raise UnsupportedCapability(("host_deployment_authorization",), profile)
    read = _RUNTIME_FACTORIES[profile].authorizations
    if read is None:
        raise UnsupportedCapability(("host_deployment_authorization",), profile)
    return read(tenant_id)


def channel_runtime_prices() -> Mapping[tuple[str, str], ProviderPrice]:
    """Read code-owned tariffs without constructing runtimes or loading credentials."""
    for registration in entry_points(group="daimon.turn_runtimes"):
        ensure_channel_runtime(registration.name)
    prices: dict[tuple[str, str], ProviderPrice] = {}
    for profile, registration in _RUNTIME_FACTORIES.items():
        if registration.tariffs is not None:
            for key, rate in registration.tariffs().items():
                if key in prices and prices[key] != rate:
                    raise UnsupportedCapability(("conflicting_runtime_price",), profile)
                prices[key] = rate
    return MappingProxyType(prices)


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
        or request.deps.turn_path != "mux"
        or not request.deps.channel_backends
    ):
        raise AdmissionDenied(reason="backend_unsupported")
    if (
        revision.channel.tenant_id != request.scope.tenant_id
        or revision.channel.platform != request.platform
        or str(request.tenant_id) != request.scope.tenant_id
        or str(request.session_account_id) != request.scope.account_id
        or request.scope.is_platform
        or request.scope.is_legacy_host_authorized
        or revision.backend != get_profile(revision.profile).provider
        or revision.model is None
    ):
        raise ScopeViolation(request.thread_id, "runtime differs from admitted channel scope")
    if not ensure_channel_runtime(revision.profile):
        raise AdmissionDenied(reason="backend_unsupported")
    return _RUNTIME_FACTORIES[revision.profile].factory(request)


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
