"""Explicit offline backend-selection callbacks observing the real Teams turn path.

The application never imports this module. No provider configuration, argument,
request, result or rendered notice is replaced. Use a dedicated idle application
with an isolated database and scripted transports, never a serving process.
"""

from __future__ import annotations

import asyncio
import inspect
import math
import uuid
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from dataclasses import dataclass

from daimon.adapters.teams import app as platform_module
from daimon.adapters.teams.app import TeamsApp
from daimon.adapters.teams.identity import TeamsInbound
from daimon.core.turn.admission import Admission, AdmissionDenied
from daimon.core.turn.prepare import PreparedTurn

_probe_active = False
_in_probe: ContextVar[bool] = ContextVar("teams_backend_probe", default=False)


@dataclass(frozen=True)
class BackendTurnEnvelope:
    app: TeamsApp
    inbound: TeamsInbound
    tenant_id: uuid.UUID
    channel_id: str
    thread_id: str
    external_user_id: str
    timeout_s: float = 30.0


async def backend_selection_turn(envelope: BackendTurnEnvelope) -> PreparedTurn:
    """Return the binding actually produced by normal ingress and execution.

    Hosts supply first/existing/new-thread inputs and CURRENT config; core's
    backend-selection collector independently reads revisions and persisted bindings. This
    callback never pins an old admission or repairs a backend selector. A real
    AdmissionDenied is re-raised after the adapter renders its normal refusal.
    Bypassed admission/binding, multiple turns and wrong scope fail closed.
    """
    global _probe_active
    app = envelope.app
    if _probe_active or app._processing or app._pending:  # pyright: ignore[reportPrivateUsage]
        raise ValueError("C14 requires a dedicated idle application and exclusive probe")
    if (
        not all((envelope.channel_id, envelope.thread_id, envelope.external_user_id))
        or not math.isfinite(envelope.timeout_s)
        or envelope.timeout_s <= 0
    ):
        raise ValueError("C14 requires declared caller/channel/thread and finite timeout")
    admissions: list[Admission] = []
    bindings: list[PreparedTurn] = []
    denials: list[AdmissionDenied] = []
    original_admit = platform_module.admit  # pyright: ignore[reportPrivateImportUsage]
    original_bind = platform_module.bind_session  # pyright: ignore[reportPrivateImportUsage]

    def relevant(call: inspect.BoundArguments) -> bool:
        return _in_probe.get() and call.arguments.get("deps") is app.runtime.turn_deps

    def validate(call: inspect.BoundArguments, *, admission: bool) -> None:
        expected: dict[str, object] = {
            "tenant_id": envelope.tenant_id,
            "platform": "teams",
            "external_user_id": envelope.external_user_id,
            "thread_id": envelope.thread_id,
        }
        if admission:
            expected["channel_id"] = envelope.channel_id
        if any(call.arguments.get(key) != value for key, value in expected.items()):
            raise ValueError("C14 production admission/binding used a different scope")

    def observe[**P, T](
        target: Callable[P, Awaitable[T]],
        received: Callable[[inspect.BoundArguments, T], None],
    ) -> Callable[P, Awaitable[T]]:
        signature = inspect.signature(target)

        async def forwarding(*args: P.args, **kwargs: P.kwargs) -> T:
            call = signature.bind(*args, **kwargs)
            try:
                result = await target(*args, **kwargs)
            except AdmissionDenied as error:
                if relevant(call):
                    validate(call, admission=target is original_admit)
                    denials.append(error)
                raise
            if relevant(call):
                received(call, result)
            return result

        return forwarding

    def admitted(call: inspect.BoundArguments, result: Admission) -> None:
        validate(call, admission=True)
        admissions.append(result)

    def bound(call: inspect.BoundArguments, result: PreparedTurn) -> None:
        validate(call, admission=False)
        if not admissions or call.arguments.get("admission") is not admissions[-1]:
            raise ValueError("C14 binding did not use the observed production admission")
        # Core may resolve the retained revision into a new Admission object.
        # It still belongs to the actual admitted caller; never pin it here.
        account = admissions[-1].account_id
        if (
            call.arguments.get("session_account_id") != account
            or result.session_account_id != account
            or result.admission.account_id != account
        ):
            raise ValueError("C14 binding returned a different caller account")
        bindings.append(result)

    _probe_active = True
    token = _in_probe.set(True)
    platform_module.admit = observe(original_admit, admitted)  # pyright: ignore[reportPrivateImportUsage]
    platform_module.bind_session = observe(original_bind, bound)  # pyright: ignore[reportPrivateImportUsage]
    try:
        async with asyncio.timeout(envelope.timeout_s):
            await app._orchestrate(envelope.inbound, envelope.tenant_id)  # pyright: ignore[reportPrivateUsage]
        if denials:
            if len(denials) != 1 or bindings:
                raise ValueError("C14 expected one refused turn without a binding")
            raise denials[0]
        if len(admissions) != 1 or len(bindings) != 1:
            raise ValueError("C14 ingress did not produce exactly one admitted and bound turn")
        return bindings[0]
    finally:
        platform_module.admit = original_admit  # pyright: ignore[reportPrivateImportUsage]
        platform_module.bind_session = original_bind  # pyright: ignore[reportPrivateImportUsage]
        _in_probe.reset(token)
        _probe_active = False
