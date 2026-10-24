"""Inline configuration and logical sessions over documented Interactions calls."""

import asyncio
import copy
import random
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from datetime import UTC, datetime, timedelta
from typing import NoReturn
from uuid import uuid4

from pydantic import JsonValue

from mux.contracts.actions import InputEvent, UserMessage, UserToolResult
from mux.contracts.config import ConfigRevision
from mux.contracts.events import Event, TextPart
from mux.contracts.ids import ModelRef, Page, PageRequest, ResourceRef, Revision, Scope, ThreadRef
from mux.contracts.receipts import (
    CancelReceipt,
    DeletionReceipt,
    Operation,
    RestoreReceipt,
    SendReceipt,
    StopObservation,
    UpdateReceipt,
)
from mux.contracts.resources import (
    Agent,
    AgentFilter,
    AgentPatch,
    AgentSpec,
    Continuity,
    Environment,
    EnvironmentFilter,
    EnvironmentPatch,
    EnvironmentSpec,
    ExportRequirements,
    ProjectionSnapshot,
    ProviderBinding,
    Session,
    SessionExport,
    SessionFilter,
    SessionSpec,
    UpdatePlan,
)
from mux.drivers.gemini.bundles import inline_files, skill_sources, target_path, validate_targets
from mux.drivers.gemini.mcp_auth import (
    LEGACY_SERVER_NAME,
    GCSSecretResolver,
    MCPSecretResolver,
    authenticated_template,
    compile_mcp,
    model_request,
    server_names,
    validate_intent,
)
from mux.drivers.gemini.normalize import OUTCOMES, actions, event, saved_events
from mux.drivers.gemini.storage import Records, SessionRecord, Storage, owner
from mux.drivers.gemini.transport import (
    NO_TOOLS_FUNCTION,
    NO_TOOLS_TOOL,
    Object,
    OwnedStream,
    Transport,
    close_iterator,
    object_value,
    string,
)
from mux.drivers.gemini.usage import observation_from_interaction
from mux.errors import (
    ContinuityLost,
    ExtensionVersionError,
    MigrationUnsupported,
    OperationConflict,
    ProviderError,
    ScopeViolation,
    UnsupportedCapability,
)
from mux.profiles.gemini import INLINE_REUSE as PROFILE
from mux.state.operations import SendClaimed, request_digest
from mux.state.store import StateStore

BASE_AGENT = "antigravity-preview-05-2026"
INTERACTION_READ_RETRY_DELAYS_S = (0.5, 1.0, 2.0, 4.0, 8.0)
"""Bounded (at most ~15.5s, jittered) backoff for transient 5xx/network failures
on interactions.get.

The interaction was already accepted, so only the read is retried; nothing is
re-created or resent. Callers bound the whole read with their own deadline.
The backoff sleeps outside the storage transaction: holding the per-account
lock for seconds would make sibling sessions on the account fail as busy.
"""


def now() -> datetime:
    return datetime.now(UTC)


def transient_read_failure(error: ProviderError) -> bool:
    """Capacity, 5xx and network failures; 4xx and malformed replies stay final."""
    return error.retryable and error.category in ("overloaded", "upstream", "transient_network")


class TransientRead(ProviderError):
    """A transient interactions.get failure, retried by `Base.retrying` after the
    storage transaction has rolled back and released its lock."""

    def __init__(self, error: ProviderError) -> None:
        super().__init__(error.category, retryable=error.retryable, native_code=error.native_code)
        self.error = error


def refuse(*features: str) -> NoReturn:
    raise UnsupportedCapability(features, PROFILE.profile_id)


def page_values[T](values: Sequence[T], page: PageRequest) -> Page[T]:
    ordered = list(values)
    if page.order == "desc":
        ordered.reverse()
    start = int(page.cursor) if page.cursor is not None else 0
    if start < 0 or start > len(ordered):
        raise ValueError("invalid page cursor")
    end = min(start + (page.limit or 100), len(ordered))
    return Page(
        data=tuple(ordered[start:end]),
        has_more=end < len(ordered),
        next_cursor=str(end) if end < len(ordered) else None,
    )


class Base:
    def __init__(
        self,
        transport: Transport,
        storage: Storage,
        account_scope_id: str,
        *,
        mcp_secrets: MCPSecretResolver | None = None,
        gcs_secrets: GCSSecretResolver | None = None,
        native_agent: Agent | None = None,
        max_total_tokens: int | None = None,
        max_output_tokens: int | None = None,
        max_input_tokens: int | None = None,
    ) -> None:
        self._max_input_tokens = max_input_tokens
        if max_input_tokens is not None and (
            type(max_input_tokens) is not int or max_input_tokens <= 0 or max_output_tokens is None
        ):
            refuse("worker_token_envelope")
        self._max_output_tokens = max_output_tokens
        self._max_total_tokens = max_total_tokens
        self._native_agent = native_agent
        self._transport, self._storage, self._account = transport, storage, account_scope_id
        self._mcp_secrets = mcp_secrets
        self._gcs_secrets = gcs_secrets

    def _mcp_request(self, records: Records, scope: Scope, spec: SessionSpec) -> Object:
        """Rebuild owned intent before authentication; never trust a saved tool list."""
        self._check(scope, spec.agent, "agent")
        agent = records.agents.get(spec.agent.id)
        if agent is None or agent.ref != spec.agent:
            raise ScopeViolation(spec.agent.id, "no owned inline agent")
        if agent.revision != spec.agent_revision:
            raise ProviderError("conflict", retryable=False)
        if (
            isinstance(agent.native, dict)
            and agent.native.get("resource_mode") == "gemini.named_agent_catalog@1"
            and self._native_agent != agent
        ):
            refuse("named_agent_session_plan")
        request = compile_agent(
            agent.spec,
            authenticated_mcp=self._mcp_secrets is not None,
            max_total_tokens=self._max_total_tokens,
            max_output_tokens=self._max_output_tokens,
        )
        if self._max_output_tokens is not None:
            if (
                (self._native_agent is not None and self._max_input_tokens is None)
                or spec.environment is not None
                or spec.resources
            ):
                refuse("model_interaction_resources")
            return request
        if self._native_agent is not None:
            if agent != self._native_agent:
                refuse("named_agent_session_plan")
            request["agent"] = agent.ref.id
        environment: JsonValue = {"type": "remote"}
        if spec.environment is not None:
            self._check(scope, spec.environment, "environment")
            env = records.environments.get(spec.environment.id)
            if env is None or env.ref != spec.environment:
                raise ScopeViolation(spec.environment.id, "no owned inline environment")
            environment = compile_environment(
                env.spec, files=inline_files(records, scope, self._account, env.spec.sources)
            )
        mounted = skill_sources(records, scope, self._account, agent.spec.skills)
        if mounted:
            configuration = object_value(environment)
            sources = configuration.get("sources", [])
            if not isinstance(sources, list):
                raise ValueError("expected inline environment sources")
            validate_targets([*sources, *mounted])
            configuration["sources"] = [*sources, *mounted]
            environment = configuration
        request["environment"] = environment
        return request

    def _ref(self, scope: Scope, id_: str, kind: str) -> ResourceRef:
        return ResourceRef(
            id=id_,
            kind=kind,
            provider="gemini",
            account_scope_id=self._account,
            tenant_id=scope.tenant_id,
            account_id=scope.account_id,
        )

    def _check(self, scope: Scope, ref: ResourceRef, kind: str) -> None:
        if (ref.provider, ref.account_scope_id, ref.kind, ref.tenant_id, ref.account_id) != (
            "gemini",
            self._account,
            kind,
            scope.tenant_id,
            scope.account_id,
        ):
            raise ScopeViolation(ref.id, "reference is outside the authorized provider scope")

    def _record(self, records: Records, scope: Scope, ref: ResourceRef) -> SessionRecord:
        self._check(scope, ref, "session")
        record = records.sessions.get(ref.id)
        if record is None or record.owner != owner(scope) or record.session.ref != ref:
            raise ScopeViolation(ref.id, "no owned logical session record")
        if record.session.state == "terminated":
            raise ContinuityLost(record.session.binding.id, ("logical session was archived",))
        if record.workspace_expires_at is not None and now() >= record.workspace_expires_at:
            raise ContinuityLost(
                record.session.binding.id,
                ("Gemini workspace expired after seven days; explicitly start a new thread.",),
            )
        return record

    def _creation(self, records: Records, scope: Scope, key: str, digest: str) -> str | None:
        deleted = records.deletions.get((*owner(scope), key))
        if deleted is not None:
            if deleted[0] != scope.principal_id:
                raise ScopeViolation(key, "operation belongs to another principal")
            raise OperationConflict(key)
        prior = records.creations.get((*owner(scope), key))
        if prior is None:
            return None
        if prior[0] != scope.principal_id:
            raise ScopeViolation(key, "operation belongs to another principal")
        if prior[1] != digest:
            raise OperationConflict(key)
        return prior[2]

    async def retrying[T](self, attempt: Callable[[], Awaitable[T]]) -> T:
        """Run `attempt` (which opens its own transaction) again after a transient read."""
        for delay in INTERACTION_READ_RETRY_DELAYS_S:
            try:
                return await attempt()
            except TransientRead:
                pass
            await asyncio.sleep(random.uniform(delay / 2, delay))
        try:
            return await attempt()
        except TransientRead as exc:
            error = exc.error
            raise ProviderError(
                error.category,
                retryable=error.retryable,
                native_code=f"{error.native_code or error.category}_read_retries_exhausted",
            ) from error

    def _bind_mount_echo(self, record: SessionRecord, interaction: str) -> None:
        """Before reading `interaction`, let the transport clear only its own echo."""
        bind = getattr(self._transport, "bind_mount_echo", None)
        digests = record.mount_echo_digests.get(interaction)
        if bind is not None and digests:
            bind(interaction, frozenset(digests))

    async def _get(self, record: SessionRecord, interaction: str) -> Object:
        """Callers run inside `retrying`, so a transient failure leaves the lock first."""
        self._bind_mount_echo(record, interaction)
        try:
            raw = await self._transport.get(interaction)
        except ProviderError as exc:
            if transient_read_failure(exc):
                raise TransientRead(exc) from exc
            if exc.category == "not_found":
                raise ContinuityLost(
                    record.session.binding.id,
                    ("Gemini history or workspace is unavailable; cannot continue.",),
                ) from None
            raise
        if string(raw["id"]) != interaction:
            raise ProviderError(
                "upstream", retryable=False, native_code="interaction_identity_mismatch"
            )
        expected = record.environment_id
        actual = raw.get("environment_id")
        if expected is not None and actual is not None and actual != expected:
            raise ContinuityLost(
                record.session.binding.id, ("Gemini replaced the reused workspace.",)
            )
        return raw

    def _observe(self, record: SessionRecord, raw: Object, root: str) -> None:
        try:
            interaction = string(raw["id"])
            # Provider chronology is independent of meter changes and survives
            # restart. Parse offsets rather than ordering timestamp strings.
            active = raw.get("updated") or raw.get("created")
            stamp: datetime | None = None
            if active is not None:
                stamp = datetime.fromisoformat(string(active).replace("Z", "+00:00"))
                if stamp.tzinfo is None:
                    raise ValueError("provider timestamp has no timezone")
                stamp = stamp.astimezone(UTC)
                watermark = record.native_updated.get(interaction)
                if watermark is not None and stamp < watermark:
                    return
                record.native_updated[interaction] = stamp
            env = raw.get("environment_id")
            if env is not None:
                record.environment_id = string(env)
            # Only execution proves activity, not merely fetching old records.
            if stamp is not None and record.environment_id is not None:
                expiry = stamp + timedelta(days=7)
                record.workspace_expires_at = max(record.workspace_expires_at or expiry, expiry)
            prior = record.observations.get(interaction)
            observation = observation_from_interaction(
                raw,
                record.session.ref,
                revision=1 if prior is None else prior.revision,
                observed_at=now(),
                root_turn=root,
                model=record.interaction_models.get(interaction, record.model),
            )
            # A meter change, or the interaction ending, is a new revision: the
            # terminal reading must reach the host even when its meter is unchanged.
            changed = prior is None or (prior.native_meter, prior.final) != (
                observation.native_meter,
                observation.final,
            )
            if prior is not None and changed:
                observation = observation.model_copy(update={"revision": prior.revision + 1})
            if changed:
                record.observations[interaction] = observation
                record.observation_history[(interaction, observation.revision)] = observation
                usage_event = event(
                    record.session.ref,
                    interaction,
                    root,
                    "usage.observed",
                    {
                        "observation_id": observation.id,
                        "revision": observation.revision,
                    },
                    identity=f"usage:{observation.revision}",
                    now=now(),
                )
                self._append(record, (usage_event,))
            normalized = saved_events(
                raw,
                record.session.ref,
                root=root,
                now=now(),
                server_names=record.mcp_server_names,
            )
            # A requires_action interaction and its result continuation share
            # one root. Only a terminal root snapshot completes it.
            self._append(record, normalized)
            state = string(raw["status"])
            if interaction == record.current:
                next_state = (
                    "idle"
                    if state in OUTCOMES
                    else ("requires_action" if state == "requires_action" else "running")
                )
                refs = dict(record.session.binding.native_refs)
                refs["interaction"] = interaction
                if record.environment_id is not None:
                    refs["environment"] = record.environment_id
                binding = record.session.binding.model_copy(update={"native_refs": refs})
                record.session = record.session.model_copy(
                    update={
                        "binding": binding,
                        "state": next_state,
                        "active_root_turn": None if state in OUTCOMES else root,
                        "required_actions": actions(raw),
                        "continuity": record.session.continuity.model_copy(
                            update={
                                "workspace_expires_at": record.workspace_expires_at,
                            }
                        ),
                    }
                )
        except (KeyError, ValueError, TypeError):
            raise ProviderError(
                "upstream", retryable=False, native_code="malformed_interaction"
            ) from None

    def _append(self, record: SessionRecord, incoming: Sequence[Event]) -> None:
        for item in incoming:
            # One terminal outcome per root, even across continuation interactions.
            if item.type == "session.turn_ended" and any(
                e.type == "session.turn_ended" and e.turn_id == item.turn_id
                for e in record.events.values()
            ):
                continue
            if item.id not in record.events:
                record.events[item.id] = item.model_copy(update={"sequence": len(record.events)})


class GeminiAgents(Base):
    async def create(self, scope: Scope, spec: AgentSpec, *, key: str) -> Agent:
        compile_agent(spec, authenticated_mcp=self._mcp_secrets is not None)
        digest = request_digest(
            {
                "kind": "agent",
                "account_scope_id": self._account,
                "spec": spec.model_dump(mode="json"),
            }
        )
        async with self._storage.transaction() as records:
            skill_sources(records, scope, self._account, spec.skills)
            id_ = self._creation(records, scope, key, digest)
            if id_ is not None:
                return records.agents[id_]
            id_ = str(uuid4())
            record = Agent(
                ref=self._ref(scope, id_, "agent"),
                revision=Revision(local=1),
                spec=spec,
                created_at=now(),
            )
            records.agents[id_] = record
            records.creations[(*owner(scope), key)] = scope.principal_id, digest, id_
            return record

    async def retrieve(self, scope: Scope, ref: ResourceRef) -> Agent:
        self._check(scope, ref, "agent")
        async with self._storage.transaction() as records:
            result = records.agents.get(ref.id)
            if result is None or result.ref != ref:
                raise ScopeViolation(ref.id, "no owned inline agent configuration")
            return result

    async def list(self, scope: Scope, *, filters: AgentFilter, page: PageRequest) -> Page[Agent]:
        async with self._storage.transaction() as records:
            return page_values(
                [
                    a
                    for a in records.agents.values()
                    if (a.ref.tenant_id, a.ref.account_id) == owner(scope)
                    and a.ref.account_scope_id == self._account
                    and (filters.name is None or a.spec.name == filters.name)
                    and (filters.include_archived or a.lifecycle == "active")
                    and (filters.created_after is None or a.created_at > filters.created_after)
                ],
                page,
            )

    async def update(
        self, scope: Scope, ref: ResourceRef, patch: AgentPatch, *, expected: Revision, key: str
    ) -> Agent:
        self._check(scope, ref, "agent")
        refuse("agent_update")

    async def archive(self, scope: Scope, ref: ResourceRef, *, key: str) -> Operation:
        self._check(scope, ref, "agent")
        refuse("agent_archive")

    async def delete(self, scope: Scope, ref: ResourceRef, *, key: str) -> DeletionReceipt:
        self._check(scope, ref, "agent")
        refuse("agent_delete")


def compile_agent(
    spec: AgentSpec,
    *,
    authenticated_mcp: bool = False,
    max_total_tokens: int | None = None,
    max_output_tokens: int | None = None,
) -> Object:
    if spec.model.provider != "gemini" or not spec.model.id or spec.model.options:
        refuse("model")
    if spec.extensions:
        refuse("agent_extensions")
    if max_output_tokens is not None:
        if max_total_tokens is not None or spec.tools or spec.skills or spec.mcp_servers:
            refuse("model_interaction_resources")
        result: Object = {
            "model": spec.model.id,
            "generation_config": {"max_output_tokens": max_output_tokens},
            "tools": [],
        }
        if spec.system is not None:
            result["system_instruction"] = spec.system
        model_request(result)
        return result
    result = {
        "agent": BASE_AGENT,
        "agent_config": {"type": "antigravity", "model": spec.model.id},
        # Omission would enable provider defaults, including billable web tools.
        "tools": [],  # filled below; never sent empty
    }
    if max_total_tokens is not None:
        if type(max_total_tokens) is not int or max_total_tokens <= 0:
            refuse("agent_token_budget")
        object_value(result["agent_config"])["max_total_tokens"] = max_total_tokens
    if spec.system is not None:
        result["system_instruction"] = spec.system
    if spec.tools is not None or spec.mcp_servers is not None:
        tools: list[JsonValue] = []
        for tool in spec.tools or ():
            if tool.permission != "auto":
                refuse("tool_confirmation")
            if tool.kind == "builtin" and tool.name in (
                "code_execution",
                "google_search",
                "url_context",
            ):
                tools.append({"type": tool.name})
            elif tool.kind == "mcp_toolset" and any(
                connection.name == tool.name for connection in spec.mcp_servers or ()
            ):
                # The neutral toolset selects its declared server. compile_mcp
                # emits the native tool once and applies auth/policy validation.
                continue
            elif tool.kind == "custom":
                if tool.name == NO_TOOLS_FUNCTION:
                    # Reserved: the host answers a call to it as "no tools here".
                    refuse("reserved_tool_name")
                tools.append(
                    {
                        "type": "function",
                        "name": tool.name,
                        "description": tool.description or "",
                        "parameters": dict(tool.input_schema or {}),
                    }
                )
            else:
                refuse("tool")
        tools.extend(compile_mcp(spec.mcp_servers or (), enabled=authenticated_mcp))
        result["tools"] = tools
    if not result["tools"]:
        # An agent's tool list is an allowlist, but an EMPTY one means Gemini's
        # defaults (code_execution, google_search, url_context; measured
        # 2026-10-23). With no tool of its own the agent allows only the inert
        # sentinel, so no built-in (billable search among them) ever runs.
        result["tools"] = [copy.deepcopy(NO_TOOLS_TOOL)]
    return result


class GeminiEnvironments(Base):
    async def create(self, scope: Scope, spec: EnvironmentSpec, *, key: str) -> Environment:
        digest = request_digest(
            {
                "kind": "environment",
                "account_scope_id": self._account,
                "spec": spec.model_dump(mode="json"),
            }
        )
        async with self._storage.transaction() as records:
            compile_environment(
                spec, files=inline_files(records, scope, self._account, spec.sources)
            )
            id_ = self._creation(records, scope, key, digest)
            if id_ is not None:
                return records.environments[id_]
            id_ = str(uuid4())
            result = Environment(
                ref=self._ref(scope, id_, "environment"),
                revision=Revision(local=1),
                spec=spec,
                created_at=now(),
            )
            records.environments[id_] = result
            records.creations[(*owner(scope), key)] = scope.principal_id, digest, id_
            return result

    async def retrieve(self, scope: Scope, ref: ResourceRef) -> Environment:
        self._check(scope, ref, "environment")
        async with self._storage.transaction() as records:
            result = records.environments.get(ref.id)
            if result is None or result.ref != ref:
                raise ScopeViolation(ref.id, "no owned inline environment configuration")
            return result

    async def list(
        self, scope: Scope, *, filters: EnvironmentFilter, page: PageRequest
    ) -> Page[Environment]:
        async with self._storage.transaction() as records:
            return page_values(
                [
                    e
                    for e in records.environments.values()
                    if (e.ref.tenant_id, e.ref.account_id) == owner(scope)
                    and e.ref.account_scope_id == self._account
                    and (filters.name is None or e.spec.name == filters.name)
                ],
                page,
            )

    async def update(
        self,
        scope: Scope,
        ref: ResourceRef,
        patch: EnvironmentPatch,
        *,
        expected: Revision,
        key: str,
    ) -> Environment:
        self._check(scope, ref, "environment")
        refuse("environment_update")

    async def archive(self, scope: Scope, ref: ResourceRef, *, key: str) -> Operation:
        self._check(scope, ref, "environment")
        refuse("environment_archive")

    async def delete(self, scope: Scope, ref: ResourceRef, *, key: str) -> DeletionReceipt:
        self._check(scope, ref, "environment")
        refuse("environment_delete")


def compile_environment(
    spec: EnvironmentSpec, *, files: dict[str, str] | None = None, model_only: bool = False
) -> Object:
    if model_only:
        if spec != EnvironmentSpec(name=spec.name, execution="none"):
            refuse("model_interaction_resources")
        return {}
    if spec.execution != "hosted" or spec.network or spec.packages or spec.native_config:
        refuse("environment_configuration")
    sources: list[JsonValue] = []
    for source in spec.sources or ():
        target = target_path(source.target_path)
        if source.kind == "file" and source.artifact is not None and files is not None:
            sources.append(
                {"type": "inline", "target": target, "content": files[source.artifact.id]}
            )
            continue
        if source.kind != "repository" or source.credential_ref or source.artifact or source.ref:
            refuse("workspace_source")
        obj: Object = {
            "type": "repository",
            "source": source.repository_url,
            "target": target,
        }
        sources.append(obj)
    validate_targets(sources)
    return {"type": "remote", "sources": sources}


class GeminiSessions(Base):
    async def create(
        self, scope: Scope, spec: SessionSpec, *, key: str, initial: UserMessage | None = None
    ) -> Session:
        self._check(scope, spec.agent, "agent")
        if spec.resources or spec.state_mode != "continue" or initial is not None:
            refuse("session_resources", "initial_input_or_fresh_state")
        ext = spec.extensions.get("gemini.session")
        if set(spec.extensions) != {"gemini.session"} or ext is None:
            refuse("gemini.session")
        if ext.version != 1:
            raise ExtensionVersionError("gemini.session", ext.version, (1,))
        if set(ext.value) != {"thread", "binding_id"}:
            raise ValueError("gemini.session accepts only thread and binding_id")
        thread = ThreadRef.model_validate(ext.value["thread"])
        if thread.channel.tenant_id != scope.tenant_id:
            raise ScopeViolation(thread.thread_id, "thread is outside tenant scope")
        digest = request_digest(
            {
                "kind": "session",
                "account_scope_id": self._account,
                "spec": spec.model_dump(mode="json"),
            }
        )
        async with self._storage.transaction() as records:
            id_ = self._creation(records, scope, key, digest)
            if id_ is not None:
                return self._record(records, scope, self._ref(scope, id_, "session")).session
            agent = records.agents.get(spec.agent.id)
            if agent is None or agent.ref != spec.agent:
                raise ScopeViolation(spec.agent.id, "no owned inline agent")
            if agent.revision != spec.agent_revision:
                raise ProviderError("conflict", retryable=False)
            request = self._mcp_request(records, scope, spec)
            id_ = str(uuid4())
            session = Session(
                ref=self._ref(scope, id_, "session"),
                binding=ProviderBinding(
                    id=string(ext.value["binding_id"]),
                    thread=thread,
                    provider="gemini",
                    profile=PROFILE.profile_id,
                    native_refs={"session": id_, "agent": spec.agent.id},
                    generation=1,
                    config_revision=spec.config_revision,
                ),
                continuity=Continuity(
                    conversation="interaction_chain",
                    workspace="native_reuse" if self._max_output_tokens is None else "none",
                    processes="unknown" if self._max_output_tokens is None else "none",
                    retention_known=True,
                ),
                requested_revision=spec.agent_revision,
                effective_revision=spec.agent_revision,
                state="idle",
            )
            records.sessions[id_] = SessionRecord(
                owner(scope),
                session,
                spec,
                agent.spec.model,
                request,
                max_input_tokens=self._max_input_tokens,
                mcp_server_names=server_names(agent.spec.mcp_servers or ()),
            )
            records.creations[(*owner(scope), key)] = scope.principal_id, digest, id_
            return session

    async def retrieve(self, scope: Scope, ref: ResourceRef) -> Session:
        return await self.retrying(lambda: self._retrieve(scope, ref))

    async def _retrieve(self, scope: Scope, ref: ResourceRef) -> Session:
        async with self._storage.transaction() as records:
            record = self._record(records, scope, ref)
            if record.current is not None:
                self._observe(
                    record,
                    await self._get(record, record.current),
                    record.interactions[record.current],
                )
            return record.session

    async def list(
        self, scope: Scope, *, filters: SessionFilter, page: PageRequest
    ) -> Page[Session]:
        if filters.created_after is not None or filters.include_archived:
            refuse("session_list_filter")
        if filters.agent is not None:
            self._check(scope, filters.agent, "agent")
        async with self._storage.transaction() as records:
            return page_values(
                [
                    r.session
                    for r in records.sessions.values()
                    if r.owner == owner(scope)
                    and r.session.state != "terminated"
                    and (filters.agent is None or r.spec.agent == filters.agent)
                ],
                page,
            )

    async def plan_update(self, scope: Scope, ref: ResourceRef, desired: SessionSpec) -> UpdatePlan:
        async with self._storage.transaction() as records:
            record = self._record(records, scope, ref)
            return UpdatePlan(
                session=ref,
                expected_revision=record.session.effective_revision,
                action="reuse" if desired == record.spec else "refuse",
                unmet=() if desired == record.spec else ("session_update",),
            )

    async def apply_update(
        self, scope: Scope, plan: UpdatePlan, *, expected: Revision, key: str
    ) -> UpdateReceipt:
        self._check(scope, plan.session, "session")
        refuse("session_update")

    async def archive(self, scope: Scope, ref: ResourceRef, *, key: str) -> Operation:
        self._check(scope, ref, "session")
        refuse("session_archive")

    async def delete(self, scope: Scope, ref: ResourceRef, *, key: str) -> DeletionReceipt:
        self._check(scope, ref, "session")
        refuse("session_delete")

    async def export(
        self, scope: Scope, ref: ResourceRef, *, requested: ExportRequirements, key: str
    ) -> SessionExport:
        self._check(scope, ref, "session")
        refuse("workspace_export_import")

    async def restore(
        self,
        scope: Scope,
        export: SessionExport,
        target: SessionSpec,
        *,
        accept_losses: frozenset[str],
        key: str,
    ) -> RestoreReceipt:
        self._check(scope, export.source, "session")
        refuse("workspace_export_import")

    async def migrate(
        self, scope: Scope, ref: ResourceRef, target: ConfigRevision, *, expected: int, key: str
    ) -> Session:
        self._check(scope, ref, "session")
        raise MigrationUnsupported(ref.id)


class GeminiEvents(Base):
    def __init__(
        self,
        transport: Transport,
        storage: Storage,
        account_scope_id: str,
        *,
        state_store: StateStore,
        model_for_interaction: Callable[[str], ModelRef | None] | None = None,
        mcp_secrets: MCPSecretResolver | None = None,
        gcs_secrets: GCSSecretResolver | None = None,
        native_agent: Agent | None = None,
        max_total_tokens: int | None = None,
        max_output_tokens: int | None = None,
        max_input_tokens: int | None = None,
    ) -> None:
        super().__init__(
            transport,
            storage,
            account_scope_id,
            mcp_secrets=mcp_secrets,
            gcs_secrets=gcs_secrets,
            native_agent=native_agent,
            max_total_tokens=max_total_tokens,
            max_output_tokens=max_output_tokens,
            max_input_tokens=max_input_tokens,
        )
        self._state_store = state_store
        self._model_for_interaction = model_for_interaction

    def _accept(
        self, record: SessionRecord, raw: Object, root: str, events: Sequence[InputEvent]
    ) -> None:
        interaction = string(raw["id"])
        if self._model_for_interaction is not None:
            selected = record.interaction_models.get(interaction) or self._model_for_interaction(
                interaction
            )
            if selected is None or selected.provider != "gemini":
                raise ProviderError(
                    "upstream", retryable=False, native_code="unknown_selected_model"
                )
            record.interaction_models[interaction] = selected
        record.current, record.root = interaction, root
        record.interactions[interaction] = root
        for index, input_ in enumerate(events):
            if isinstance(input_, UserMessage):
                self._append(
                    record,
                    (
                        event(
                            record.session.ref,
                            interaction,
                            root,
                            "user.message",
                            {
                                "input_id": f"{interaction}:input:{index}",
                                "content": [p.model_dump(mode="json") for p in input_.content],
                            },
                            identity=f"input:{index}",
                            now=now(),
                        ),
                    ),
                )
        self._observe(record, raw, root)

    async def send(
        self,
        scope: Scope,
        session: ResourceRef,
        events: Sequence[InputEvent],
        *,
        key: str,
        expected_turn: str | None = None,
    ) -> SendReceipt:
        # Every history read precedes begin_operation, so a retried attempt
        # starts from a rolled-back transaction and can never dispatch twice.
        return await self.retrying(
            lambda: self._send(scope, session, events, key=key, expected_turn=expected_turn)
        )

    async def _send(
        self,
        scope: Scope,
        session: ResourceRef,
        events: Sequence[InputEvent],
        *,
        key: str,
        expected_turn: str | None,
    ) -> SendReceipt:
        self._check(scope, session, "session")
        if not events:
            raise ValueError("empty input")
        digest = request_digest(
            {
                "session": session.model_dump(mode="json"),
                "events": [e.model_dump(mode="json") for e in events],
                "expected_turn": expected_turn,
            }
        )
        async with self._storage.transaction() as records:
            record = self._record(records, scope, session)
            expected = self._mcp_request(records, scope, record.spec)
            agent = records.agents[record.spec.agent.id]
            connections = agent.spec.mcp_servers or ()
            try:
                validate_intent(
                    record.request, expected, connections, enabled=self._mcp_secrets is not None
                )
            except UnsupportedCapability as exc:
                if exc.missing != (LEGACY_SERVER_NAME,):
                    raise
                raise ContinuityLost(
                    record.session.binding.id,
                    (
                        "This Gemini conversation was saved with an MCP server name Gemini "
                        "no longer accepts (mcp_server_name); start a new thread.",
                    ),
                ) from None
            prior = records.sends.get((*owner(scope), key))
            if prior is not None:
                if prior[0] != scope.principal_id:
                    raise ScopeViolation(key, "operation belongs to another principal")
                if prior[1] != digest:
                    raise OperationConflict(key)
                if prior[2].status == "outcome_unknown":
                    operation = await self._state_store.get_operation(scope, key)
                    if (
                        operation is not None
                        and operation.operation.status in ("accepted", "processed")
                        and operation.result
                    ):
                        receipt = SendReceipt.model_validate(operation.result)
                        if (
                            operation.operation.resource != session
                            or not receipt.input_ids
                            or receipt.turn_id is None
                        ):
                            raise ProviderError(
                                "upstream", retryable=False, native_code="invalid_acknowledgement"
                            )
                        # A restored acknowledgement still reads native history.
                        # Recheck scoped admission before that I/O, without a POST.
                        await authenticated_template(
                            scope,
                            record.request,
                            expected,
                            connections,
                            self._mcp_secrets,
                            overrides={},
                            gcs_secrets=self._gcs_secrets,
                        )
                        raw = await self._get(record, receipt.input_ids[0])
                        self._accept(record, raw, receipt.turn_id, events)
                        record.unknown_delivery = False
                        records.sends[(*owner(scope), key)] = scope.principal_id, digest, receipt
                        return receipt
                return prior[2]
            if record.unknown_delivery:
                refuse("unreconciled_delivery")
            if self._max_output_tokens is not None and any(
                not isinstance(event, UserMessage) for event in events
            ):
                refuse("model_interaction_resources")
            if record.max_input_tokens != self._max_input_tokens:
                refuse("changed_worker_token_envelope")
            if (self._max_total_tokens is not None or self._max_input_tokens is not None) and (
                record.current is not None
                or len(events) != 1
                or not isinstance(events[0], UserMessage)
                or events[0].mode != "new_turn"
            ):
                refuse("worker_single_interaction")
            if expected_turn is not None and expected_turn != record.session.active_root_turn:
                raise ProviderError("conflict", retryable=False)
            overrides: Object = {}
            inputs: list[JsonValue] = []
            is_result = all(isinstance(e, UserToolResult) for e in events)
            if record.session.state == "running":
                refuse("steer")
            if record.session.state == "requires_action" and not is_result:
                refuse("required_action_pending")
            if is_result:
                for input_ in events:
                    if not isinstance(input_, UserToolResult):
                        refuse("function_result")
                    action = next(
                        (a for a in record.session.required_actions if a.id == input_.action_id),
                        None,
                    )
                    if action is None or action.kind != "function_result":
                        refuse("unknown_function_action")
                    result_input: Object = {
                        "type": "function_result",
                        "name": action.payload["name"],
                        "call_id": action.call_id,
                        "result": _text(input_.content),
                    }
                    if input_.is_error:
                        result_input["is_error"] = True
                    inputs.append(result_input)
            else:
                for input_ in events:
                    if not isinstance(input_, UserMessage) or input_.mode != "new_turn":
                        refuse("native_input_or_confirmation_or_steer")
                    inputs.append({"type": "text", "text": _text(input_.content)})
            overrides.update({"input": inputs, "background": True, "store": True})
            authenticated = any(c.credential_ref is not None for c in connections)
            if record.current is not None:
                if not authenticated:
                    await self._get(record, record.current)
                if self._max_output_tokens is None and record.environment_id is None:
                    raise ContinuityLost(
                        record.session.binding.id,
                        ("Gemini did not provide a reusable environment ID.",),
                    )
                overrides["previous_interaction_id"] = record.current
                if self._max_output_tokens is None:
                    overrides["environment"] = record.environment_id
            request = await authenticated_template(
                scope,
                record.request,
                expected,
                connections,
                self._mcp_secrets,
                overrides=overrides,
                gcs_secrets=self._gcs_secrets,
            )
            if record.current is not None and authenticated:
                # Admission/authentication precedes this native history read.
                await self._get(record, record.current)
            if self._max_input_tokens is not None:
                from mux.drivers.gemini.transport import TokenCountingTransport

                if not isinstance(self._transport, TokenCountingTransport):
                    refuse("worker_input_token_count")
                count = await self._transport.count_input_tokens(request)
                if type(count) is not int or count <= 0 or count > self._max_input_tokens:
                    refuse("worker_input_token_limit")
            root = record.root if is_result else str(uuid4())
            if root is None:
                refuse("unknown_function_action")
            begun = await self._state_store.begin_operation(
                scope,
                key=key,
                request_digest=digest,
                operation_id=key,
                now=now(),
            )
            if begun.record.operation.status != "pending":
                if begun.record.result:
                    return SendReceipt.model_validate(begun.record.result)
                return SendReceipt(operation_id=key, status="outcome_unknown", input_ids=())
            try:
                await self._state_store.claim_send(scope, key, now=now(), fence=None)
            except SendClaimed:
                return SendReceipt(operation_id=key, status="outcome_unknown", input_ids=())
            # Commit uncertainty before I/O. A crash or rollback cannot turn
            # an accepted POST into a fresh request, even under a different key.
            unknown = SendReceipt(operation_id=key, status="outcome_unknown", input_ids=())
            record.unknown_delivery = True
            records.sends[(*owner(scope), key)] = scope.principal_id, digest, unknown
        try:
            raw = await self._transport.create(request)
        except ProviderError as exc:
            if exc.category in ("transient_network", "upstream", "overloaded"):
                await self._state_store.advance_operation(
                    scope, key, "outcome_unknown", now=now(), fence=None
                )
                return unknown
            # Definite refusal permits a new operation, but never retries this key.
            await self._state_store.advance_operation(scope, key, "failed", now=now(), fence=None)
            async with self._storage.transaction(after_dispatch=True) as records:
                record = self._record(records, scope, session)
                record.unknown_delivery = False
            if exc.category == "not_found" and request.get("previous_interaction_id") is not None:
                raise ContinuityLost(
                    record.session.binding.id,
                    ("Gemini reused environment expired or history was lost.",),
                ) from None
            raise
        try:
            string(raw["id"])
            string(raw["status"])
        except (KeyError, ValueError, TypeError):
            raise ProviderError(
                "upstream", retryable=False, native_code="malformed_interaction"
            ) from None
        async with self._storage.transaction(after_dispatch=True) as records:
            record = self._record(records, scope, session)
            interaction = string(raw["id"])
            if not is_result:
                root = interaction
            if record.environment_id is not None and raw.get("environment_id") not in (
                None,
                record.environment_id,
            ):
                raise ContinuityLost(
                    record.session.binding.id, ("Gemini silently changed the reused environment.",)
                )
            receipt = SendReceipt(
                operation_id=key, status="queued", input_ids=(interaction,), turn_id=root
            )
            await self._state_store.advance_operation(
                scope,
                key,
                "accepted",
                now=now(),
                fence=None,
                resource=session,
                result=receipt.model_dump(mode="json"),
            )
            # Persist the mount-header digests with the acceptance, before any read.
            dispatched = getattr(self._transport, "mount_echo_digests", None)
            if dispatched is not None and (sent := dispatched(interaction)):
                record.mount_echo_digests[interaction] = sorted(sent)
            self._accept(record, raw, root, events)
            record.unknown_delivery = False
            records.sends[(*owner(scope), key)] = scope.principal_id, digest, receipt
            return receipt

    async def open_stream(
        self,
        scope: Scope,
        session: ResourceRef,
        *,
        after: str | None = None,
        previews: bool = False,
    ) -> AsyncIterator[Event]:
        async with self._storage.transaction() as records:
            record = self._record(records, scope, session)
            interaction, root = record.current, record.root
            if interaction is not None:
                self._bind_mount_echo(record, interaction)
        if interaction is None or root is None:

            async def empty() -> AsyncIterator[Event]:
                if False:
                    yield  # pragma: no cover

            return empty()
        source = await self._transport.open_stream(interaction, after=after)

        async def normalized() -> AsyncIterator[Event]:
            preview_sequence = 0
            delivered: set[str] = set()
            try:
                async for raw in source:
                    kind = raw.get("event_type")
                    if kind == "step.delta" and previews:
                        delta = object_value(raw["delta"])
                        if delta.get("type") == "text":
                            yield event(
                                session,
                                interaction,
                                root,
                                "agent.message.delta",
                                {
                                    "item_id": f"step:{raw.get('index', 0)}",
                                    "content_index": 0,
                                    "text": string(delta["text"]),
                                    "preview_sequence": preview_sequence,
                                },
                                identity=str(raw.get("event_id") or f"preview:{preview_sequence}"),
                                now=now(),
                                preview=True,
                                native_type="step.delta",
                            )
                            preview_sequence += 1
                    elif kind in ("interaction.completed", "interaction.status_update"):
                        # Status events alone have no saved final content. Fetch
                        # canonical steps; EOF or a child/native event is never an end.
                        await self.reconcile(scope, session)
                        async with self._storage.transaction() as records:
                            current = self._record(records, scope, session)
                            batch = tuple(e for e in current.events.values() if e.turn_id == root)
                        for item in batch:
                            if item.id not in delivered:
                                delivered.add(item.id)
                                yield item
                    elif kind == "error":
                        gap = event(
                            session,
                            interaction,
                            root,
                            "session.history_gap",
                            {
                                "domain": interaction,
                                "after": after,
                                # Saved steps recover final content, but cannot
                                # prove lossless replay of the interrupted SSE domain.
                                "recoverable": False,
                            },
                            identity=str(raw.get("event_id") or "stream_error"),
                            now=now(),
                            native_type="error",
                        )
                        async with self._storage.transaction() as records:
                            current = self._record(records, scope, session)
                            self._append(current, (gap,))
                            gap = current.events[gap.id]
                        if gap.id not in delivered:
                            delivered.add(gap.id)
                            yield gap
            finally:
                close = getattr(source, "aclose", None)
                if close is not None:
                    await close()

        async def close_source() -> None:
            await close_iterator(source)

        return OwnedStream(normalized(), close_source)

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
            async for item in source:
                yield item
        finally:
            close = getattr(source, "aclose", None)
            if close is not None:
                await close()

    async def list(self, scope: Scope, session: ResourceRef, *, page: PageRequest) -> Page[Event]:
        async with self._storage.transaction() as records:
            return page_values(tuple(self._record(records, scope, session).events.values()), page)

    async def reconcile(self, scope: Scope, session: ResourceRef) -> ProjectionSnapshot:
        return await self.retrying(lambda: self._reconcile(scope, session))

    async def _reconcile(self, scope: Scope, session: ResourceRef) -> ProjectionSnapshot:
        async with self._storage.transaction() as records:
            record = self._record(records, scope, session)
            for interaction, root in record.interactions.items():
                self._observe(record, await self._get(record, interaction), root)
            s = record.session
            return ProjectionSnapshot(
                session=session,
                cursor=str(len(record.events)),
                state=s.state if s.state != "provisioning" else "running",
                active_root_turn=s.active_root_turn,
                required_actions=s.required_actions,
                gaps=tuple(e.id for e in record.events.values() if e.type == "session.history_gap"),
                taken_at=now(),
            )

    async def cancel(
        self, scope: Scope, session: ResourceRef, *, turn_id: str, key: str
    ) -> CancelReceipt:
        async with self._storage.transaction() as records:
            record = self._record(records, scope, session)
            if record.root != turn_id or record.current is None:
                raise ProviderError("conflict", retryable=False)
            status = "already_stopped" if record.session.state == "idle" else "requested"
            digest = request_digest(
                {"kind": "cancel", "session": session.model_dump(mode="json"), "turn_id": turn_id}
            )
            begun = await self._state_store.begin_operation(
                scope, key=key, request_digest=digest, operation_id=key, now=now()
            )
            if begun.record.operation.status != "pending":
                if begun.record.result:
                    return CancelReceipt.model_validate(begun.record.result)
                return CancelReceipt(
                    operation_id=key,
                    session=session,
                    turn_id=turn_id,
                    status="outcome_unknown",
                    requested_at=begun.record.operation.created_at,
                )
            await self._state_store.claim_send(scope, key, now=now(), fence=None)
            if status == "requested":
                try:
                    self._bind_mount_echo(record, record.current)
                    await self._transport.cancel(record.current)
                except ProviderError as exc:
                    if exc.category != "transient_network":
                        raise
                    status = "outcome_unknown"
            receipt = CancelReceipt(
                operation_id=key,
                session=session,
                turn_id=turn_id,
                status=status,
                requested_at=now(),
            )
            await self._state_store.advance_operation(
                scope,
                key,
                "outcome_unknown" if status == "outcome_unknown" else "processed",
                now=now(),
                fence=None,
                result=receipt.model_dump(mode="json"),
            )
            return receipt

    async def wait_stopped(
        self, scope: Scope, receipt: CancelReceipt, *, deadline: datetime
    ) -> StopObservation:
        self._check(scope, receipt.session, "session")
        if now() >= deadline:
            return StopObservation(
                receipt_operation_id=receipt.operation_id, stopped=False, observed_at=now()
            )
        try:
            async with asyncio.timeout(max(0, (deadline - now()).total_seconds())):
                return await self.retrying(lambda: self._observe_stop(scope, receipt))
        except TimeoutError:
            return StopObservation(
                receipt_operation_id=receipt.operation_id, stopped=False, observed_at=now()
            )

    async def _observe_stop(self, scope: Scope, receipt: CancelReceipt) -> StopObservation:
        async with self._storage.transaction() as records:
            record = self._record(records, scope, receipt.session)
            if record.root != receipt.turn_id or record.current is None:
                raise ProviderError("conflict", retryable=False)
            raw = await self._get(record, record.current)
            self._observe(record, raw, receipt.turn_id)
            outcome = OUTCOMES.get(string(raw["status"]))
            return StopObservation(
                receipt_operation_id=receipt.operation_id,
                stopped=outcome is not None,
                outcome=outcome,
                observed_at=now(),
            )


def _text(parts: Sequence[object]) -> str:
    if any(not isinstance(p, TextPart) for p in parts):
        refuse("input_modality")
    return "".join(p.text for p in parts if isinstance(p, TextPart))
