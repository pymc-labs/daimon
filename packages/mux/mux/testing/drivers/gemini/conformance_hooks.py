"""Explicit conformance recovery seam; never installed by the host factory."""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Literal

from mux.contracts.actions import InputEvent
from mux.contracts.ids import ResourceRef, Scope
from mux.contracts.ports import ManagedAgents
from mux.contracts.receipts import SendReceipt
from mux.contracts.resources import (
    Agent,
    AgentSpec,
    Environment,
    EnvironmentSpec,
    Session,
    SessionSpec,
    Skill,
    SkillUpload,
)
from mux.errors import OperationConflict, ProviderError, ScopeViolation, UnsupportedCapability
from mux.profiles.gemini import INLINE_REUSE
from mux.state.operations import check_owner, request_digest
from mux.state.store import StateStore, binding_slot


@dataclass(frozen=True)
class SendRecoveryEvidence:
    receipt: SendReceipt
    basis: Literal["durable_acknowledgement", "native_lookup_unavailable"]
    request_digest: str


@dataclass(frozen=True)
class InlineSkillDeployment:
    """Local preparation; native deployment is proved by the later POST bytes."""

    skill: Skill
    agent: Agent
    environment: Environment
    desired: SessionSpec
    session: Session


async def prepare_inline_skill(
    driver: ManagedAgents,
    scope: Scope,
    bundle: SkillUpload,
    *,
    agent: AgentSpec,
    environment: EnvironmentSpec,
    session_spec: Callable[[Agent, Environment], SessionSpec],
    key: str,
) -> InlineSkillDeployment:
    """Prepare exactly one pinned bundle through ordinary scoped driver ports.

    The caller supplies the host's binding/thread configuration. This seam does
    not select a driver, discover credentials, send input or claim that Gemini
    has an upstream skills upload. The first interaction deploys the inline
    sources; a conformance recorder must independently retain those wire facts.
    """
    if (
        driver.capabilities().profile_id != INLINE_REUSE.profile_id
        or agent.model.provider != "gemini"
        or agent.skills
    ):
        raise UnsupportedCapability(("single_inline_skill",), INLINE_REUSE.profile_id)
    skill = await driver.skills.create(scope, bundle, key=f"{key}:skill")
    pin = skill.latest_version
    if pin is None or pin.version is None:
        raise ProviderError("upstream", retryable=False, native_code="missing_skill_pin")
    deployed = await driver.agents.create(
        scope, agent.model_copy(update={"skills": (pin,)}), key=f"{key}:agent"
    )
    env = await driver.environments.create(scope, environment, key=f"{key}:environment")
    desired = session_spec(deployed, env)
    if (
        desired.agent != deployed.ref
        or desired.agent_revision != deployed.revision
        or desired.environment != env.ref
    ):
        raise ScopeViolation(key, "session preparation changed the pinned deployment")
    session = await driver.sessions.create(scope, desired, key=f"{key}:session")
    return InlineSkillDeployment(skill, deployed, env, desired, session)


async def recover_send(
    driver: ManagedAgents,
    store: StateStore,
    scope: Scope,
    session: ResourceRef,
    events: Sequence[InputEvent],
    *,
    key: str,
    expected_turn: str | None = None,
) -> SendRecoveryEvidence:
    """Recover an existing claimed operation, never create or redeliver intent.

    Gemini cannot look up an interaction by the host's operation key. A lost
    acknowledgement therefore stays unknown, even if another interaction has
    identical text. Only the original durable acknowledgement can name input
    IDs. Ordinary session reads establish ownership, not native key correlation.
    Unclaimed/failed operations require a separately authorized send decision.
    """
    events = tuple(events)
    if (
        driver.capabilities().profile_id != INLINE_REUSE.profile_id
        or session.provider != "gemini"
        or not events
    ):
        raise ValueError("Gemini recovery requires a session and immutable input")
    owned = await driver.sessions.retrieve(scope, session)
    record = await store.get_operation(scope, key)
    if record is None:
        raise OperationConflict(key)
    check_owner(record, scope)
    digest = request_digest(
        {
            "session": session.model_dump(mode="json"),
            "events": [event.model_dump(mode="json") for event in events],
            "expected_turn": expected_turn,
        }
    )
    if record.operation.key != key or record.operation.request_digest != digest:
        raise OperationConflict(key)
    if record.slot is not None and record.slot != binding_slot(owned.binding):
        raise ScopeViolation(key, "operation belongs to another binding")
    if record.operation.status in ("pending", "failed"):
        raise UnsupportedCapability(("explicit_send_decision",), INLINE_REUSE.profile_id)
    if record.operation.status in ("sent", "outcome_unknown"):
        # No provider query or retry could recover this key's native identity.
        return SendRecoveryEvidence(
            SendReceipt(operation_id=record.operation.id, status="outcome_unknown", input_ids=()),
            "native_lookup_unavailable",
            digest,
        )
    receipt = SendReceipt.model_validate(record.result)
    if (
        record.operation.resource != session
        or receipt.operation_id != record.operation.id
        or not receipt.input_ids
        or receipt.turn_id is None
        or receipt.status not in ("queued", "processed")
        or (receipt.status == "processed" and record.operation.status != "processed")
    ):
        raise ProviderError("upstream", retryable=False, native_code="invalid_acknowledgement")
    return SendRecoveryEvidence(receipt, "durable_acknowledgement", digest)
