"""Explicitly constructed Gemini inline-reuse driver; never a resolved default."""

from mux.contracts.admission import Admission, admit
from mux.contracts.config import ConfigRevision
from mux.contracts.ids import Page, PageRequest, ResourceRef, Scope
from mux.contracts.ports import Artifacts, Models, Skills
from mux.contracts.profile import Profile
from mux.contracts.usage import UsageObservation
from mux.drivers.gemini.core import (
    Base,
    GeminiAgents,
    GeminiEnvironments,
    GeminiEvents,
    GeminiSessions,
    page_values,
)
from mux.drivers.gemini.storage import Storage
from mux.drivers.gemini.transport import Transport
from mux.errors import UnsupportedCapability
from mux.profiles.gemini import INLINE_REUSE as PROFILE
from mux.state.store import StateStore


class GeminiUsage(Base):
    async def reconcile(self, scope: Scope, session: ResourceRef) -> tuple[UsageObservation, ...]:
        async with self._storage.transaction() as records:
            record = self._record(records, scope, session)
            for interaction, root in record.interactions.items():
                self._observe(record, await self._get(record, interaction), root)
            return tuple(record.observation_history.values())

    async def list(
        self, scope: Scope, session: ResourceRef, *, page: PageRequest
    ) -> Page[UsageObservation]:
        async with self._storage.transaction() as records:
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
    ) -> None:
        self.agents = GeminiAgents(transport, storage, account_scope_id)
        self.environments = GeminiEnvironments(transport, storage, account_scope_id)
        self.sessions = GeminiSessions(transport, storage, account_scope_id)
        self.events = GeminiEvents(transport, storage, account_scope_id, state_store=state_store)
        self.usage = GeminiUsage(transport, storage, account_scope_id)

    @property
    def artifacts(self) -> Artifacts:
        raise UnsupportedCapability(("artifacts",), PROFILE.profile_id)

    @property
    def skills(self) -> Skills:
        raise UnsupportedCapability(("skills_bundle",), PROFILE.profile_id)

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


__all__ = ["GeminiManagedAgents"]
