"""Per-admitted-channel runtime construction, without startup credential access."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from typing import TYPE_CHECKING

from daimon.core.mux_backend import TurnRuntime
from daimon.core.turn.errors import AdmissionDenied
from mux.errors import ScopeViolation
from mux.profiles import get_profile

if TYPE_CHECKING:
    from daimon.core.turn.prepare import ProviderPreparationRequest

RuntimeFactory = Callable[["ProviderPreparationRequest"], TurnRuntime]
_RUNTIME_FACTORIES: dict[str, RuntimeFactory] = {}


def register_channel_runtime(profile: str, factory: RuntimeFactory) -> None:
    """Register an explicit deployment constructor; registration performs no I/O."""
    if profile == "anthropic.managed_agents" or profile in _RUNTIME_FACTORIES:
        raise ValueError(f"channel runtime already registered: {profile}")
    _RUNTIME_FACTORIES[profile] = factory


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
    factory = _RUNTIME_FACTORIES.get(revision.profile)
    if factory is None and revision.profile == "openai.persistent_workspace":
        from daimon.core.turn.openai_deployment import build_openai_runtime

        factory = build_openai_runtime
    if factory is None:
        raise AdmissionDenied(reason="backend_unsupported")
    return factory(request)


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
        return len(tuple(self))
