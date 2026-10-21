"""Turn lifecycle over C3, with durable replay as the recovery authority."""

from __future__ import annotations

import asyncio
import random
import re
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from datetime import UTC, datetime, timedelta
from typing import Literal, Protocol
from urllib.parse import urlencode
from uuid import NAMESPACE_URL, uuid5

from pydantic import ValidationError

from mux.contracts.actions import InputEvent, UserMessage, UserToolConfirmation, UserToolResult
from mux.contracts.events import (
    Event,
    ImagePart,
    RequiredAction,
    RequiresActionPayload,
    TextPart,
    TurnEndedPayload,
)
from mux.contracts.ids import Page, PageRequest, ResourceRef, Scope, SkillRef
from mux.contracts.receipts import CancelReceipt, SendReceipt, StopObservation
from mux.contracts.resources import AgentSpec, ProjectionSnapshot
from mux.contracts.usage import UsageObservation
from mux.drivers.selfhosted import wire
from mux.drivers.selfhosted.context import Context
from mux.drivers.selfhosted.normalize import normalize, usage
from mux.drivers.selfhosted.sessions import SelfHostedSessions
from mux.drivers.selfhosted.transport import secure_url
from mux.errors import (
    ContinuityLost,
    OperationConflict,
    ProviderError,
    ScopeViolation,
    UnsupportedCapability,
)
from mux.state.operations import request_digest

SkillResolver = Callable[[Scope, SkillRef], Awaitable[wire.Skill]]


class TurnTools(Protocol):
    @property
    def mcp_servers(self) -> Sequence[wire.MCPServer]: ...
    @property
    def skills(self) -> Sequence[wire.Skill]: ...


ToolsResolver = Callable[[Scope, AgentSpec], Awaitable[TurnTools]]
HighWater = Callable[[ResourceRef], Awaitable[int]]


def segment(value: str) -> str:
    from urllib.parse import quote

    return quote(value, safe="")


def agent_tool_policy(spec: AgentSpec) -> dict[str, Literal["auto", "ask", "deny"]]:
    """Compile the pinned agent permissions before adding host safety gates."""
    builtin_names = {"bash", "read", "write", "edit"}
    policy: dict[str, Literal["auto", "ask", "deny"]] = {}
    if spec.tools is not None:
        if any(
            tool.kind == "custom" or (tool.kind == "builtin" and tool.name not in builtin_names)
            for tool in spec.tools
        ):
            raise UnsupportedCapability(("custom_tools_or_tool_permissions",), "selfhosted.loop")
        policy.update(dict.fromkeys(builtin_names, "deny"))
        for tool in spec.tools:
            name = tool.name + "/*" if tool.kind == "mcp_toolset" else tool.name
            policy[name] = tool.permission
    for connection in spec.mcp_servers or ():
        configured = connection.tool_policy
        allowed = configured.get("allowed_tools")
        default = policy.get(connection.name + "/*", "auto")
        if "allowed_tools" in configured:
            if not isinstance(allowed, list) or any(
                not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+", name)
                for name in allowed
            ):
                raise UnsupportedCapability(("mcp_allowed_tools",), "selfhosted.loop")
            policy[connection.name + "/*"] = "deny"
            for name in allowed:
                policy[connection.name + "/" + str(name)] = default
        for name, permission in configured.items():
            if name == "allowed_tools":
                continue
            if permission not in ("auto", "ask", "deny"):
                raise UnsupportedCapability(("mcp_tool_policy",), "selfhosted.loop")
            qualified = (
                name if name.startswith(connection.name + "/") else connection.name + "/" + name
            )
            if "/" in name and not name.startswith(connection.name + "/"):
                raise UnsupportedCapability(("mcp_tool_policy_scope",), "selfhosted.loop")
            tool_name = qualified.removeprefix(connection.name + "/")
            if isinstance(allowed, list) and tool_name not in allowed and permission != "deny":
                raise UnsupportedCapability(
                    ("mcp_tool_policy_widens_allowlist",), "selfhosted.loop"
                )
            policy[qualified] = permission
    return policy


class SelfHostedEvents:
    def __init__(
        self,
        context: Context,
        sessions: SelfHostedSessions,
        *,
        model: str | None = None,
        deployment: str | None = None,
        max_tool_calls: int = 100,
        deadline_s: float = 300,
        skill_resolver: SkillResolver | None = None,
        tools_resolver: ToolsResolver | None = None,
        high_water: HighWater | None = None,
    ) -> None:
        self._c, self._sessions = context, sessions
        self._deployment = deployment
        self._normalized: dict[ResourceRef, dict[int, Event]] = {}
        self.unresolved_turns: dict[ResourceRef, set[str]] = {}
        self._skill_resolver = skill_resolver
        self._tools_resolver = tools_resolver
        self.high_water = high_water
        self._high_water: dict[ResourceRef, int] = {}
        if max_tool_calls < 0 or deadline_s <= 0:
            raise ValueError("invalid turn limits")
        self._model, self._max_tool_calls, self._deadline_s = model, max_tool_calls, deadline_s
        self._history: dict[ResourceRef, dict[int, wire.Event]] = {}
        self._sends: dict[tuple[ResourceRef, str], tuple[str, SendReceipt]] = {}
        self._locks: dict[ResourceRef, asyncio.Lock] = {}
        self._intents: dict[tuple[ResourceRef, str], str] = {}
        self._approvals: dict[tuple[ResourceRef, str, str], tuple[str, SendReceipt]] = {}
        self._cancels: dict[tuple[ResourceRef, str], CancelReceipt] = {}
        self._stream_cursors: dict[ResourceRef, int] = {}

    def _remember(self, ref: ResourceRef, raw: wire.Event) -> Event | None:
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
                        if prior.model_dump(
                            exclude={"observed_at", "native_meter"}
                        ) == observation.model_dump(exclude={"observed_at", "native_meter"}):
                            # Keep the raw seq for continuity, but deliver and
                            # account for this logical model response only once.
                            history[raw.seq] = raw
                            return None
                        raise ProviderError(
                            "upstream", retryable=False, native_code="duplicate_usage"
                        )
        normalized = normalize(raw, ref)
        history[raw.seq] = raw
        if raw.type == "turn_ended" and not any(
            observation.turn_id == raw.turn_id for observation in self.observations(ref)
        ):
            self.unresolved_turns.setdefault(ref, set()).add(raw.turn_id)
            payload = TurnEndedPayload(
                root_turn_id=raw.turn_id,
                outcome="interrupted" if raw.data.get("outcome") == "interrupted" else "errored",
                native_reason="turn_usage_required",
            )
            normalized = normalized.model_copy(update={"payload": payload.model_dump(mode="json")})
        if raw.type == "requires_action":
            prior_events = tuple(self._normalized.get(ref, {}).values()) + (normalized,)
            actions = self.projection(ref, prior_events, False).required_actions
            normalized = normalized.model_copy(
                update={"payload": RequiresActionPayload(actions=actions).model_dump(mode="json")}
            )
        self._normalized.setdefault(ref, {})[raw.seq] = normalized
        return normalized

    async def cursor(self, session: ResourceRef) -> int:
        persisted = await self.high_water(session) if self.high_water is not None else 0
        value = max(
            persisted,
            self._high_water.get(session, 0),
            self._stream_cursors.get(session, 0),
            max(self._history.get(session, {}), default=0),
        )
        if not 0 <= value <= 2**53:
            raise ValueError("invalid session high-water")
        self._high_water[session] = value
        return value

    async def replay(
        self, scope: Scope, ref: ResourceRef, *, deadline: datetime | None = None
    ) -> tuple[tuple[Event, ...], bool]:
        binding = self._c.binding(scope, ref)
        spec = await self._sessions.spec(scope, ref)
        if spec.environment is None:
            raise self._c.unsupported("selfhosted_environment_required")
        environment = await self._c.environments.retrieve(scope, spec.environment)
        if environment.lifecycle != "active" or environment.revision != self._c.revision(
            binding, "environment"
        ):
            raise ContinuityLost(binding.id, ("environment changed since session creation",))
        self._c.check_environment(environment)
        after = await self.cursor(ref)
        replay_after = max(self._history.get(ref, {}), default=0)
        query = {"after_seq": after}
        if replay_after < after:
            query["replay_after_seq"] = replay_after
        raw = await self._c.transport.request(
            "GET",
            f"/v0/sessions/{segment(ref.id)}/events?{urlencode(query)}",
            session_id=ref.id,
            after_seq=after,
            deadline=deadline,
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
            if event.seq not in self._history.get(ref, {}):
                if remembered := self._remember(ref, event):
                    result.append(remembered)
            elif self._history[ref][event.seq] != event:
                raise ContinuityLost(ref.id, ("harness changed a committed event",))
            last = event.seq
        result = list(self._normalized.get(ref, {}).values())
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
        actions: dict[str, RequiredAction] = {}
        for event in events:
            if event.type == "session.status_running":
                if active != event.turn_id:
                    actions.clear()
                state, active = "running", event.turn_id
            elif event.type == "session.requires_action":
                state, active = "requires_action", event.turn_id
                for action in RequiresActionPayload.model_validate(event.payload).actions:
                    actions[action.id] = action
            elif event.type == "session.turn_ended":
                state, active = "idle", None
                actions.clear()
            elif event.type in ("agent.tool_use", "agent.tool_result"):
                call_id = event.payload.get("call_id")
                actions = {
                    key: action for key, action in actions.items() if action.call_id != call_id
                }
                state, active = "running", event.turn_id
        return ProjectionSnapshot.model_validate(
            {
                "session": ref,
                "cursor": str(events[-1].sequence) if events else "0",
                "state": "requires_action" if actions else state,
                "active_root_turn": active,
                "required_actions": tuple(actions.values()),
                "gaps": tuple(event.id for event in events if event.type == "session.history_gap"),
                "taken_at": datetime.now(UTC),
            }
        )

    async def reconcile(self, scope: Scope, session: ResourceRef) -> ProjectionSnapshot:
        events, complete = await self.replay(scope, session)
        snapshot = self.projection(session, events, complete)
        ended = {event.turn_id for event in events if event.type == "session.turn_ended"}
        accepted = next(
            (
                receipt.turn_id
                for (ref, _), (_, receipt) in self._sends.items()
                if ref == session and receipt.turn_id not in ended
            ),
            None,
        )
        if snapshot.active_root_turn is None and accepted is not None:
            snapshot = snapshot.model_copy(
                update={"state": "running", "active_root_turn": accepted}
            )
        return snapshot

    async def _request(
        self, scope: Scope, session: ResourceRef, message: UserMessage, key: str
    ) -> wire.TurnRequest:
        if not message.content:
            raise ProviderError("invalid_request", retryable=False, native_code="empty_input")
        spec = await self._sessions.spec(scope, session)
        agent = await self._c.agents.retrieve(scope, spec.agent)
        self._c.check(scope, agent.ref, "agent")
        binding = self._c.binding(scope, session)
        if agent.ref != spec.agent or agent.revision != self._c.revision(binding, "agent"):
            raise ContinuityLost(binding.id, ("agent changed since session creation",))
        if spec.environment is None:
            raise self._c.unsupported("selfhosted_environment_required")
        environment = await self._c.environments.retrieve(scope, spec.environment)
        self._c.check(scope, environment.ref, "environment")
        if (
            environment.ref != spec.environment
            or environment.lifecycle != "active"
            or environment.revision != self._c.revision(binding, "environment")
        ):
            raise ContinuityLost(binding.id, ("environment changed since session creation",))
        self._c.check_environment(environment)
        if agent.lifecycle != "active" or agent.spec.model.provider != "selfhosted":
            raise self._c.unsupported("agent_lifecycle_or_provider")
        if agent.spec.model.options or agent.spec.extensions:
            raise self._c.unsupported("agent_options_or_extensions")
        policy = agent_tool_policy(agent.spec)
        model = self._model or agent.spec.model.id
        if self._deployment is not None and model != self._deployment:
            raise self._c.unsupported("model_deployment_mismatch")
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
                if connection.transport != "streamable_http":
                    raise self._c.unsupported("mcp_policy_or_transport")
                headers: dict[str, str] = {}
                if connection.credential_ref is not None:
                    raise self._c.unsupported("credentialed_mcp_requires_scoped_tools_resolver")
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
            if server.headers_env and self._tools_resolver is None:
                raise self._c.unsupported("credentialed_mcp_requires_scoped_tools_resolver")
            if server.name in names or any(
                not (
                    re.fullmatch(r"HARNESS_MCP_CRED_[A-Za-z0-9_]+", name)
                    or name == "DAIMON_MCP_AUTHORIZATION"
                )
                for name in server.headers_env.values()
            ):
                raise self._c.unsupported("mcp_server_names_or_credential_references")
            names.add(server.name)
            if (server.headers_env or server.name == "daimon-mcp") and not secure_url(server.url):
                raise self._c.unsupported("credential_mcp_requires_https")
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
        total_bytes = 0
        for skill in skills:
            if not isinstance(skill, wire.InlineSkill):
                raise self._c.unsupported("unbounded_url_skill")
            size = sum(map(len, skill.decoded_files().values()))
            if size > wire.SKILL_MAX_BYTES:
                raise self._c.unsupported("skill_size_limit")
            total_bytes += size
        if total_bytes > wire.TURN_SKILLS_MAX_BYTES:
            raise self._c.unsupported("turn_skills_size_limit")
        turn_id = "sh_turn_" + str(uuid5(NAMESPACE_URL, session.model_dump_json() + key))
        return wire.TurnRequest(
            session_id=session.id,
            turn_id=turn_id,
            idempotency_key=turn_id,
            model=model,
            tool_policy=policy,
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
        mcp_authorization: str | None = None,
    ) -> SendReceipt:
        self._c.binding(scope, session)
        async with self._locks.setdefault(session, asyncio.Lock()):
            return await self._send(
                scope,
                session,
                events,
                key=key,
                expected_turn=expected_turn,
                mcp_authorization=mcp_authorization,
            )

    async def _send(
        self,
        scope: Scope,
        session: ResourceRef,
        events: Sequence[InputEvent],
        *,
        key: str,
        expected_turn: str | None,
        mcp_authorization: str | None,
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
            if prior[1].status != "outcome_unknown":
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
            try:
                raw = await self._c.transport.request(
                    "POST",
                    f"/v0/turns?after_seq={await self.cursor(session)}",
                    session_id=session.id,
                    after_seq=await self.cursor(session),
                    body=request.model_dump(mode="json"),
                    mcp_authorization=mcp_authorization,
                )
            except ProviderError as error:
                if error.native_code != "malformed_response":
                    raise
                raw = None
            if (
                not isinstance(raw, dict)
                or raw.get("accepted") is not True
                or raw.get("turn_id") != request.turn_id
            ):
                receipt = SendReceipt(
                    operation_id=key,
                    status="outcome_unknown",
                    input_ids=(key,),
                    turn_id=request.turn_id,
                )
                self._sends[session, key] = digest, receipt
                return receipt
            turn_id = request.turn_id
        elif isinstance(event, UserToolConfirmation | UserToolResult):
            values, _ = await self.replay(scope, session)
            requested = next(
                (
                    raw
                    for raw in reversed(tuple(self._history.get(session, {}).values()))
                    if raw.type == "requires_action"
                    and raw.data.get("action_id") == event.action_id
                    and (expected_turn is None or raw.turn_id == expected_turn)
                ),
                None,
            )
            if requested is None:
                raise ProviderError("conflict", retryable=False, native_code="action_not_pending")
            turn_id = requested.turn_id
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
            action_digest = request_digest(body.model_dump(mode="json", exclude_unset=True))
            action_key = (session, turn_id, event.action_id)
            attempted = self._approvals.get(action_key)
            if attempted is not None and attempted[0] != action_digest:
                raise OperationConflict(key)
            settled = any(
                value.turn_id == turn_id
                and value.type == "agent.tool_result"
                and value.payload.get("call_id") == requested.data.get("call_id")
                for value in values
            )
            snapshot = self.projection(session, values, False)
            pending = any(action.id == event.action_id for action in snapshot.required_actions)
            if settled:
                receipt = SendReceipt(
                    operation_id=key, status="processed", input_ids=(key,), turn_id=turn_id
                )
            elif not pending:
                receipt = SendReceipt(
                    operation_id=key, status="outcome_unknown", input_ids=(key,), turn_id=turn_id
                )
            else:
                receipt = SendReceipt(
                    operation_id=key, status="outcome_unknown", input_ids=(key,), turn_id=turn_id
                )
                # Keep the decision before HTTP. Retrying the identical body is
                # safe because the harness deduplicates actions by their id.
                self._approvals[action_key] = action_digest, receipt
                self._sends[session, key] = digest, receipt
                try:
                    await self._c.transport.request(
                        "POST",
                        f"/v0/turns/{segment(turn_id)}/actions"
                        f"?after_seq={await self.cursor(session)}",
                        body=body.model_dump(mode="json", exclude_unset=True),
                        session_id=session.id,
                        after_seq=await self.cursor(session),
                    )
                except ProviderError as error:
                    if not error.retryable:
                        raise
                    try:
                        values, _ = await self.replay(scope, session)
                    except ProviderError as replay_error:
                        if not replay_error.retryable:
                            raise
                    settled = any(
                        value.turn_id == turn_id
                        and value.type == "agent.tool_result"
                        and value.payload.get("call_id") == requested.data.get("call_id")
                        for value in values
                    )
                    if settled:
                        receipt = receipt.model_copy(update={"status": "processed"})
                else:
                    receipt = receipt.model_copy(update={"status": "queued"})
            self._approvals[action_key] = action_digest, receipt
            self._sends[session, key] = digest, receipt
            return receipt
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
        if not 0 <= cursor <= 2**53:
            raise ValueError("invalid session cursor")
        self._high_water[session] = max(self._high_water.get(session, 0), cursor)
        # Each HTTP request is bounded by the transport. An idle reader may
        # wait for a later turn, whose execution and cleanup have their own limits.
        initial, complete = await self.replay(
            scope, session, deadline=datetime.now(UTC) + timedelta(seconds=30)
        )
        if after is None:
            cursor = self._stream_cursors.get(session, 0)
            active = self.projection(session, initial, complete).active_root_turn
            if active is not None:
                active_events = [event.sequence for event in initial if event.turn_id == active]
                if active_events:
                    cursor = max(cursor, min(active_events) - 1)

        async def iterate() -> AsyncIterator[Event]:
            nonlocal cursor
            replayed, done = initial, complete
            observed_end = False
            delay = 0.1
            while True:
                for event in replayed:
                    if event.sequence > cursor:
                        cursor = event.sequence
                        self._stream_cursors[session] = max(
                            self._stream_cursors.get(session, 0), cursor
                        )
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
                await asyncio.sleep(random.uniform(0.1, delay))
                replayed, done = await self.replay(
                    scope, session, deadline=datetime.now(UTC) + timedelta(seconds=30)
                )
                delay = (
                    0.1
                    if any(event.sequence > cursor for event in replayed)
                    else min(2.0, delay * 2)
                )

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
                "POST",
                f"/v0/turns/{segment(turn_id)}/cancel?after_seq={await self.cursor(session)}",
                body={},
                session_id=session.id,
                after_seq=await self.cursor(session),
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
        delay = 0.1
        while (deadline - datetime.now(UTC)).total_seconds() > 0:
            try:
                values, _ = await self.replay(scope, receipt.session, deadline=deadline)
            except ProviderError as error:
                if error.native_code == "polling_deadline":
                    break
                raise
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
            await asyncio.sleep(max(0, min(random.uniform(0.1, delay), remaining)))
            delay = min(2.0, delay * 2)
        return StopObservation(
            receipt_operation_id=receipt.operation_id, stopped=False, observed_at=datetime.now(UTC)
        )

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
