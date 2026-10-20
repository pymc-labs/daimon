"""Turn lifecycle over C3, with durable replay as the recovery authority."""

from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from datetime import UTC, datetime
from typing import Protocol
from uuid import NAMESPACE_URL, uuid5

from pydantic import ValidationError

from mux.contracts.actions import InputEvent, UserMessage, UserToolConfirmation, UserToolResult
from mux.contracts.events import Event, ImagePart, RequiresActionPayload, TextPart, TurnEndedPayload
from mux.contracts.ids import Page, PageRequest, ResourceRef, Scope, SkillRef
from mux.contracts.receipts import CancelReceipt, SendReceipt, StopObservation
from mux.contracts.resources import AgentSpec, ProjectionSnapshot
from mux.contracts.usage import UsageObservation
from mux.drivers.selfhosted import wire
from mux.drivers.selfhosted.context import Context
from mux.drivers.selfhosted.normalize import normalize, usage
from mux.drivers.selfhosted.sessions import SelfHostedSessions
from mux.errors import ContinuityLost, OperationConflict, ProviderError, ScopeViolation
from mux.state.operations import request_digest

SkillResolver = Callable[[Scope, SkillRef], Awaitable[wire.Skill]]


class TurnTools(Protocol):
    @property
    def mcp_servers(self) -> Sequence[wire.MCPServer]: ...
    @property
    def skills(self) -> Sequence[wire.Skill]: ...


ToolsResolver = Callable[[Scope, AgentSpec], Awaitable[TurnTools]]


def segment(value: str) -> str:
    from urllib.parse import quote

    return quote(value, safe="")


class SelfHostedEvents:
    def __init__(
        self,
        context: Context,
        sessions: SelfHostedSessions,
        *,
        model: str | None = None,
        max_tool_calls: int = 100,
        deadline_s: float = 300,
        skill_resolver: SkillResolver | None = None,
        tools_resolver: ToolsResolver | None = None,
    ) -> None:
        self._c, self._sessions = context, sessions
        self._skill_resolver = skill_resolver
        self._tools_resolver = tools_resolver
        if max_tool_calls < 0 or deadline_s <= 0:
            raise ValueError("invalid turn limits")
        self._model, self._max_tool_calls, self._deadline_s = model, max_tool_calls, deadline_s
        self._history: dict[ResourceRef, dict[int, wire.Event]] = {}
        self._sends: dict[tuple[ResourceRef, str], tuple[str, SendReceipt]] = {}
        self._locks: dict[ResourceRef, asyncio.Lock] = {}
        self._intents: dict[tuple[ResourceRef, str], str] = {}
        self._cancels: dict[tuple[ResourceRef, str], CancelReceipt] = {}

    def _remember(self, ref: ResourceRef, raw: wire.Event) -> Event:
        if raw.session_id != ref.id:
            raise ScopeViolation(ref.id, "harness replay contains a foreign session")
        history = self._history.setdefault(ref, {})
        previous = history.get(raw.seq)
        if previous is not None and previous != raw:
            raise ContinuityLost(ref.id, ("harness changed a committed event",))
        if raw.type == "usage":
            observation = usage(raw, ref)
            for old in history.values():
                if old.seq != raw.seq and old.type == "usage":
                    prior = usage(old, ref)
                    if prior.id == observation.id:
                        raise ProviderError(
                            "upstream", retryable=False, native_code="duplicate_usage"
                        )
        if raw.type == "turn_ended" and not any(
            (
                event.type in ("usage", "model_request_started")
                or (
                    event.type == "error"
                    and event.data.get("kind") in ("usage_unavailable", "usage_missing")
                )
            )
            and event.turn_id == raw.turn_id
            for event in history.values()
        ):
            raise ProviderError("upstream", retryable=False, native_code="turn_usage_required")
        normalized = normalize(raw, ref)
        history[raw.seq] = raw
        return normalized

    async def replay(self, scope: Scope, ref: ResourceRef) -> tuple[tuple[Event, ...], bool]:
        self._c.binding(scope, ref)
        after = max(self._history.get(ref, {}), default=0)
        raw = await self._c.transport.request(
            "GET",
            f"/v0/sessions/{segment(ref.id)}/events?after_seq={after}",
            session_id=ref.id,
        )
        try:
            response = wire.EventsResponse.model_validate(raw)
        except ValidationError:
            raise ProviderError(
                "upstream", retryable=False, native_code="malformed_replay"
            ) from None
        if len(response.events) > 100_000:
            raise ProviderError("upstream", retryable=False, native_code="replay_limit")
        result: list[Event] = []
        last = -1
        for event in response.events:
            if event.seq <= last:
                raise ProviderError("upstream", retryable=False, native_code="unordered_replay")
            result.append(self._remember(ref, event))
            last = event.seq
        result = [
            normalize(event, ref)
            for event in sorted(self._history.get(ref, {}).values(), key=lambda event: event.seq)
        ]
        pending = {
            receipt.turn_id
            for (session, _), (_, receipt) in self._sends.items()
            if session == ref and receipt.turn_id is not None
        }
        ended = {event.turn_id for event in result if event.type == "session.turn_ended"}
        return tuple(result), response.complete and pending.issubset(ended)

    @staticmethod
    def projection(ref: ResourceRef, events: Sequence[Event], complete: bool) -> ProjectionSnapshot:
        state = "idle" if complete else "running"
        active: str | None = None
        actions = ()
        for event in events:
            if event.type == "session.status_running":
                state, active, actions = "running", event.turn_id, ()
            elif event.type == "session.requires_action":
                state, active = "requires_action", event.turn_id
                actions = RequiresActionPayload.model_validate(event.payload).actions
            elif event.type == "session.turn_ended":
                state, active, actions = "idle", None, ()
            elif event.type in ("agent.tool_use", "agent.tool_result", "agent.message"):
                state, active, actions = "running", event.turn_id, ()
        return ProjectionSnapshot.model_validate(
            {
                "session": ref,
                "cursor": str(events[-1].sequence) if events else "0",
                "state": state,
                "active_root_turn": active,
                "required_actions": actions,
                "taken_at": datetime.now(UTC),
            }
        )

    async def reconcile(self, scope: Scope, session: ResourceRef) -> ProjectionSnapshot:
        events, complete = await self.replay(scope, session)
        return self.projection(session, events, complete)

    async def _request(
        self, scope: Scope, session: ResourceRef, message: UserMessage, key: str
    ) -> wire.TurnRequest:
        if not message.content:
            raise ProviderError("invalid_request", retryable=False, native_code="empty_input")
        spec = await self._sessions.spec(scope, session)
        agent = await self._c.agents.retrieve(scope, spec.agent)
        self._c.check(scope, agent.ref, "agent")
        binding = self._c.binding(scope, session)
        if agent.ref != spec.agent or str(agent.revision.local) != binding.native_refs.get(
            "agent_revision"
        ):
            raise ContinuityLost(binding.id, ("agent changed since session creation",))
        if spec.environment is None:
            raise self._c.unsupported("selfhosted_environment_required")
        environment = await self._c.environments.retrieve(scope, spec.environment)
        self._c.check(scope, environment.ref, "environment")
        if (
            environment.ref != spec.environment
            or environment.lifecycle != "active"
            or str(environment.revision.local) != binding.native_refs.get("environment_revision")
        ):
            raise ContinuityLost(binding.id, ("environment changed since session creation",))
        self._c.check_environment(environment)
        if agent.lifecycle != "active" or agent.spec.model.provider != "selfhosted":
            raise self._c.unsupported("agent_lifecycle_or_provider")
        if agent.spec.model.options or agent.spec.extensions:
            raise self._c.unsupported("agent_options_or_extensions")
        if agent.spec.tools is not None:
            builtins = {tool.name for tool in agent.spec.tools if tool.kind == "builtin"}
            if builtins != {"bash", "read", "write", "edit"} or any(
                tool.permission != "auto" or tool.kind == "custom" for tool in agent.spec.tools
            ):
                raise self._c.unsupported("custom_tools_or_tool_permissions")
        parts: list[wire.Part] = []
        for part in message.content:
            if isinstance(part, TextPart):
                parts.append(wire.TextPart(text=part.text))
            elif isinstance(part, ImagePart) and part.data_base64 is not None:
                parts.append(wire.ImagePart(media_type=part.media_type, data_b64=part.data_base64))
            else:
                raise self._c.unsupported("input_part")
        if self._tools_resolver is not None:
            tools = await self._tools_resolver(scope, agent.spec)
            servers, skills = list(tools.mcp_servers), list(tools.skills)
        else:
            servers: list[wire.MCPServer] = []
            for connection in agent.spec.mcp_servers or ():
                if connection.tool_policy or connection.transport != "streamable_http":
                    raise self._c.unsupported("mcp_policy_or_transport")
                headers: dict[str, str] = {}
                if connection.credential_ref is not None:
                    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", connection.credential_ref):
                        raise self._c.unsupported("mcp_credential_environment_name")
                    headers["Authorization"] = connection.credential_ref
                servers.append(
                    wire.MCPServer(name=connection.name, url=connection.url, headers_env=headers)
                )
            skills: list[wire.Skill] = []
            for skill in agent.spec.skills or ():
                if self._skill_resolver is None:
                    raise self._c.unsupported("skills_bundle")
                if skill.version is not None:
                    raise self._c.unsupported("skills_versions")
                skills.append(await self._skill_resolver(scope, skill))
        names: set[str] = set()
        for server in servers:
            if server.name in names or any(
                not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name)
                for name in server.headers_env.values()
            ):
                raise self._c.unsupported("mcp_server_names_or_credential_references")
            names.add(server.name)
            if (
                "DAIMON_MCP_AUTHORIZATION" in server.headers_env.values()
                and server.name != "daimon-mcp"
            ):
                raise self._c.unsupported("reserved_mcp_credential")
            if (
                server.name == "daimon-mcp"
                and server.headers_env.get("Authorization") != "DAIMON_MCP_AUTHORIZATION"
            ):
                raise self._c.unsupported("reserved_mcp_credential")
        turn_id = "sh_turn_" + str(uuid5(NAMESPACE_URL, session.model_dump_json() + key))
        return wire.TurnRequest(
            session_id=session.id,
            turn_id=turn_id,
            idempotency_key=turn_id,
            model=self._model or agent.spec.model.id,
            instructions=agent.spec.system or "",
            input=parts,
            mcp_servers=servers,
            skills=skills,
            limits=wire.Limits(max_tool_calls=self._max_tool_calls, deadline_s=self._deadline_s),
        )

    async def send(
        self,
        scope: Scope,
        session: ResourceRef,
        events: Sequence[InputEvent],
        *,
        key: str,
        expected_turn: str | None = None,
    ) -> SendReceipt:
        self._c.binding(scope, session)
        async with self._locks.setdefault(session, asyncio.Lock()):
            return await self._send(scope, session, events, key=key, expected_turn=expected_turn)

    async def _send(
        self,
        scope: Scope,
        session: ResourceRef,
        events: Sequence[InputEvent],
        *,
        key: str,
        expected_turn: str | None,
    ) -> SendReceipt:
        if len(events) != 1:
            raise self._c.unsupported("batched_inputs")
        digest = request_digest(
            {"events": [e.model_dump(mode="json") for e in events], "expected_turn": expected_turn}
        )
        self._intent(session, key, digest)
        prior = self._sends.get((session, key))
        if prior:
            if prior[0] != digest:
                raise OperationConflict(key)
            return prior[1]
        event = events[0]
        if isinstance(event, UserMessage):
            if event.mode != "new_turn":
                raise self._c.unsupported("steer")
            request = await self._request(scope, session, event, key)
            snapshot = await self.reconcile(scope, session)
            if (
                snapshot.active_root_turn is not None
                and snapshot.active_root_turn != request.turn_id
            ):
                raise ProviderError("conflict", retryable=False, native_code="turn_already_running")
            if expected_turn is not None and expected_turn != request.turn_id:
                raise ProviderError(
                    "conflict", retryable=False, native_code="expected_turn_mismatch"
                )
            raw = await self._c.transport.request(
                "POST", "/v0/turns", session_id=session.id, body=request.model_dump(mode="json")
            )
            if (
                not isinstance(raw, dict)
                or raw.get("accepted") is not True
                or raw.get("turn_id") != request.turn_id
            ):
                raise ProviderError("upstream", retryable=False, native_code="invalid_turn_ack")
            turn_id = request.turn_id
        elif isinstance(event, UserToolConfirmation | UserToolResult):
            snapshot = await self.reconcile(scope, session)
            action = next((a for a in snapshot.required_actions if a.id == event.action_id), None)
            if (
                action is None
                or snapshot.active_root_turn is None
                or (expected_turn is not None and expected_turn != snapshot.active_root_turn)
            ):
                raise ProviderError("conflict", retryable=False, native_code="action_not_pending")
            turn_id = snapshot.active_root_turn
            if isinstance(event, UserToolConfirmation):
                body = wire.ActionRequest(action_id=event.action_id, decision=event.decision)
            else:
                body = wire.ActionRequest(
                    action_id=event.action_id,
                    decision="result",
                    result={
                        "content": [part.model_dump(mode="json") for part in event.content],
                        "is_error": event.is_error,
                    },
                )
            await self._c.transport.request(
                "POST",
                f"/v0/turns/{segment(turn_id)}/actions",
                body=body.model_dump(mode="json", exclude_unset=True),
                session_id=session.id,
            )
        else:
            raise self._c.unsupported("native_input")
        receipt = SendReceipt(operation_id=key, status="queued", input_ids=(key,), turn_id=turn_id)
        self._sends[session, key] = digest, receipt
        return receipt

    async def open_stream(
        self,
        scope: Scope,
        session: ResourceRef,
        *,
        after: str | None = None,
        previews: bool = False,
    ) -> AsyncIterator[Event]:
        self._c.binding(scope, session)
        cursor = int(after or "0")
        # Establish the first durable HTTP replay request before returning;
        # opening on an idle session must still wait for the next turn.
        initial, complete = await self.replay(scope, session)

        async def iterate() -> AsyncIterator[Event]:
            nonlocal cursor
            replayed, done = initial, complete
            observed_end = False
            while True:
                for event in replayed:
                    if event.sequence > cursor:
                        cursor = event.sequence
                        observed_end = observed_end or event.type == "session.turn_ended"
                        if previews or event.authority != "preview":
                            yield event
                ended = {event.turn_id for event in replayed if event.type == "session.turn_ended"}
                pending = {
                    receipt.turn_id
                    for (ref, _), (_, receipt) in self._sends.items()
                    if ref == session and receipt.turn_id is not None
                }
                if done and observed_end and pending.issubset(ended):
                    return
                await asyncio.sleep(0.1)
                replayed, done = await self.replay(scope, session)

        return iterate()

    async def stream(
        self,
        scope: Scope,
        session: ResourceRef,
        *,
        after: str | None = None,
        previews: bool = False,
    ) -> AsyncIterator[Event]:
        source = await self.open_stream(scope, session, after=after, previews=previews)
        try:
            async for event in source:
                yield event
        finally:
            close = getattr(source, "aclose", None)
            if close is not None:
                await close()

    async def list(self, scope: Scope, session: ResourceRef, *, page: PageRequest) -> Page[Event]:
        values, _ = await self.replay(scope, session)
        after = int(page.cursor or "0")
        if page.order == "desc":
            selected = tuple(
                e for e in reversed(values) if page.cursor is None or e.sequence < after
            )
        else:
            selected = tuple(e for e in values if e.sequence > after)
        count = page.limit or 100
        more = len(selected) > count
        selected = selected[:count]
        return Page(
            data=selected, has_more=more, next_cursor=str(selected[-1].sequence) if more else None
        )

    def _intent(self, session: ResourceRef, key: str, digest: str) -> None:
        previous = self._intents.get((session, key))
        if previous is not None and previous != digest:
            raise OperationConflict(key)
        self._intents[session, key] = digest

    async def cancel(
        self, scope: Scope, session: ResourceRef, *, turn_id: str, key: str
    ) -> CancelReceipt:
        self._c.binding(scope, session)
        self._intent(session, key, request_digest({"kind": "cancel", "turn_id": turn_id}))
        prior = self._cancels.get((session, key))
        if prior is not None:
            return prior
        values, _ = await self.replay(scope, session)
        ended = any(e.turn_id == turn_id and e.type == "session.turn_ended" for e in values)
        known_send = any(
            ref == session and receipt.turn_id == turn_id
            for (ref, _), (_, receipt) in self._sends.items()
        )
        if not ended and not known_send and not any(e.turn_id == turn_id for e in values):
            raise ScopeViolation(turn_id, "cancel target is not in this session")
        status = "already_stopped"
        if not ended:
            raw = await self._c.transport.request(
                "POST", f"/v0/turns/{segment(turn_id)}/cancel", body={}, session_id=session.id
            )
            result = wire.CancelResponse.model_validate(raw)
            status = "requested" if result.cancelled else "outcome_unknown"
        receipt = CancelReceipt.model_validate(
            {
                "operation_id": key,
                "session": session,
                "turn_id": turn_id,
                "status": status,
                "requested_at": datetime.now(UTC),
            }
        )

        self._cancels[session, key] = receipt
        return receipt

    async def wait_stopped(
        self, scope: Scope, receipt: CancelReceipt, *, deadline: datetime
    ) -> StopObservation:
        while True:
            values, _ = await self.replay(scope, receipt.session)
            for event in values:
                if event.turn_id == receipt.turn_id and event.type == "session.turn_ended":
                    payload = TurnEndedPayload.model_validate(event.payload)
                    return StopObservation(
                        receipt_operation_id=receipt.operation_id,
                        stopped=True,
                        outcome=payload.outcome,
                        observed_at=datetime.now(UTC),
                    )
            remaining = (deadline - datetime.now(UTC)).total_seconds()
            if remaining <= 0:
                return StopObservation(
                    receipt_operation_id=receipt.operation_id,
                    stopped=False,
                    observed_at=datetime.now(UTC),
                )
            await asyncio.sleep(min(0.1, remaining))

    def observations(self, session: ResourceRef) -> tuple[UsageObservation, ...]:
        observations: dict[str, UsageObservation] = {}
        for event in sorted(self._history.get(session, {}).values(), key=lambda event: event.seq):
            if event.type in ("usage", "model_request_started") or (
                event.type == "error"
                and event.data.get("kind") in ("usage_unavailable", "usage_missing")
            ):
                observation = usage(event, session)
                previous = observations.get(observation.id)
                if previous is None or observation.revision > previous.revision:
                    observations[observation.id] = observation
        return tuple(observations.values())

    async def aclose(self) -> None:
        """Polling holds no outstanding connection between calls."""
