"""Session continuity follows the declared profile and the host's binding."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from mux.contracts.actions import UserMessage
from mux.contracts.config import ConfigRevision
from mux.contracts.ids import ChannelRef, Page, PageRequest, ResourceRef, Revision, Scope, ThreadRef
from mux.contracts.receipts import DeletionReceipt, Operation, RestoreReceipt, UpdateReceipt
from mux.contracts.resources import (
    Continuity,
    ExportRequirements,
    ProviderBinding,
    Session,
    SessionExport,
    SessionFilter,
    SessionSpec,
    UpdatePlan,
)
from mux.drivers.openai._common import Context, objects, owned, query, text
from mux.drivers.openai.actions import content
from mux.drivers.openai.mounts import resources, vault_ids
from mux.drivers.openai.normalize import required_actions
from mux.drivers.openai.session_controls import SessionControls
from mux.drivers.openai.skill_bindings import compile_pins, decode, encode_metadata, resolve_pins
from mux.drivers.openai.transport import Object, object_json, segment
from mux.errors import ContinuityLost, MigrationUnsupported, ProviderError, ScopeViolation

BindingLookup = Callable[[Scope, str], ProviderBinding | None]
_DELETE_TIMEOUT = 30.0
_DELETE_POLL_INTERVAL = 1.0


class OpenAISessions:
    def __init__(
        self,
        context: Context,
        binding_lookup: BindingLookup | None = None,
        *,
        controls: SessionControls | None = None,
    ) -> None:
        self._c, self._binding_lookup = context, binding_lookup
        self._controls = controls if controls is not None else SessionControls()

    def _binding(self, scope: Scope, raw: Object) -> ProviderBinding:
        id_ = text(raw["id"])
        binding = self._binding_lookup(scope, id_) if self._binding_lookup is not None else None
        if binding is not None:
            if (
                binding.provider != "openai"
                or binding.profile != self._c.profile_id
                or binding.native_refs.get("session") != id_
                or binding.thread.channel.tenant_id != scope.tenant_id
            ):
                raise ScopeViolation(
                    id_, "binding does not match provider session, profile or tenant"
                )
            return binding
        metadata = object_json(raw.get("metadata") or {})
        refs: dict[str, str] = {"session": id_}
        environment = object_json(raw.get("environment") or {})
        if environment.get("id") is not None:
            refs["environment"] = text(environment["id"])
        agent = object_json(raw["agent"])
        refs["agent"] = text(agent["id"])
        return ProviderBinding(
            id=id_,
            thread=ThreadRef(
                channel=ChannelRef(
                    tenant_id=scope.tenant_id,
                    platform="openai",
                    channel_id=text(metadata.get("mux_channel") or id_),
                ),
                thread_id=text(metadata.get("mux_thread") or id_),
            ),
            provider="openai",
            profile=self._c.profile_id,
            native_refs=refs,
            generation=0,
            config_revision=int(text(metadata.get("mux_config_revision") or "0")),
            legacy_account_id=scope.account_id,
        )

    async def _decode(self, scope: Scope, raw: Object) -> Session:
        self._c.record(scope, raw)
        binding = self._binding(scope, raw)
        expected_agent = binding.native_refs.get("agent")
        if expected_agent is not None and object_json(raw["agent"]).get("id") != expected_agent:
            raise ContinuityLost(binding.id, ("session agent identity changed",))
        environment = object_json(raw["environment"])
        persistent = self._c.profile_id == "openai.persistent_workspace"
        if persistent and environment.get("type") != "openai_hosted":
            raise ContinuityLost(binding.id, ("persistent hosted workspace is unavailable",))
        if not persistent and environment.get("type") != "none":
            raise ContinuityLost(binding.id, ("conversation-only environment mode changed",))
        expected_env = binding.native_refs.get("environment")
        if expected_env is not None and environment.get("id") != expected_env:
            raise ContinuityLost(binding.id, ("hosted environment identity changed",))
        pins = decode(object_json(raw.get("metadata") or {}))
        if pins is not None:
            actual = objects(environment.get("skills", []))
            identities = {
                (value.get("skill_id"), value.get("version"))
                for value in actual
                if value.get("type") == "skill_reference"
            }
            expected = {(pin.id, pin.version) for pin in pins}
            if identities != expected or len(actual) != len(pins):
                raise ContinuityLost(
                    binding.id, ("installed skill pins changed or are unavailable",)
                )
        if self._controls.model is not None:
            agent = object_json(raw["agent"])
            if agent.get("model") != self._controls.model:
                raise ContinuityLost(binding.id, ("session model differs from admitted model",))
        status = text(raw["status"])
        states = {
            "idle": "idle",
            "in_progress": "running",
            "requires_action": "requires_action",
            "failed": "terminated",
        }
        if status not in states:
            raise ValueError("unknown native session status")
        active: str | None = None
        if status in ("in_progress", "requires_action"):
            page = await self._c.call(
                "GET",
                f"/agents/sessions/{segment(text(raw['id']))}/turns",
                params={"order": "desc", "limit": 100},
            )
            for turn in objects(page["data"]):
                if turn.get("session_id") != raw["id"]:
                    raise ValueError("active turn belongs to another session")
                if (
                    turn.get("subagent_id") is None
                    and "subagent_id" in turn
                    and turn.get("status") in ("queued", "in_progress", "waiting")
                ):
                    active = text(turn["id"])
                    break
            if active is None:
                raise ProviderError(
                    "upstream", retryable=True, native_code="active_root_unavailable"
                )
        rev = Revision(local=binding.config_revision)
        return Session.model_validate(
            {
                "ref": self._c.ref(scope, "session", text(raw["id"])),
                "binding": binding,
                "continuity": Continuity(
                    conversation="native_session",
                    workspace="native_reuse" if persistent else "none",
                    processes="unknown" if persistent else "none",
                ),
                "requested_revision": rev,
                "effective_revision": rev,
                "state": states[status],
                "active_root_turn": active,
                "required_actions": required_actions(raw),
            }
        )

    @owned
    async def create(
        self, scope: Scope, spec: SessionSpec, *, key: str, initial: UserMessage | None = None
    ) -> Session:
        self._c.authorize(scope, "session")
        self._c.check(scope, spec.agent, "agent")
        if set(spec.extensions) - {"openai.vaults"}:
            raise self._c.unsupported("session_extension")
        files = resources(scope, spec.resources, self._c)
        vaults = (
            vault_ids(scope, spec.extensions["openai.vaults"], self._c)
            if "openai.vaults" in spec.extensions
            else None
        )
        if spec.agent_revision.local != 0 or spec.agent_revision.native is not None:
            raise self._c.unsupported("agent_revision_pin")
        if self._c.profile_id == "openai.conversation_only":
            if self._controls.container_size is not None:
                raise self._c.unsupported("hosted_container_size")
            if spec.environment is not None or spec.resources:
                raise self._c.unsupported("thread_workspace_persistence")
            if initial is None or not initial.content:
                raise ProviderError(
                    "invalid_request", retryable=False, native_code="initial_input_required"
                )
            environment: Object = {"type": "none"}
        else:
            environment = {"type": "openai_hosted"}
            if self._controls.container_size is not None:
                environment["container_size"] = self._controls.container_size
            if spec.environment is not None:
                self._c.check(scope, spec.environment, "environment")
                environment["environment_template_id"] = spec.environment.id
        if files:
            environment["files"] = [value for value in files]
        agent = await self._c.call("GET", "/agents/" + segment(spec.agent.id))
        if agent.get("id") != spec.agent.id:
            raise ValueError("wrong session agent identity")
        self._c.record(scope, agent)
        pins = decode(agent.get("metadata"))
        resolved = None
        if pins is not None:
            if self._c.profile_id == "openai.conversation_only" and pins:
                raise self._c.unsupported("skills_bundle")
            resolved = await resolve_pins(pins, scope, self._c)
            if self._c.profile_id != "openai.conversation_only":
                environment["skills"] = compile_pins(resolved, scope, self._c)
        metadata: Object = {
            **dict(spec.metadata),
            "mux_tenant": scope.tenant_id,
            "mux_config_revision": str(spec.config_revision),
        }
        if resolved is not None:
            metadata.update(encode_metadata(resolved, self._c))
        body: Object = {"agent_id": spec.agent.id, "environment": environment, "metadata": metadata}
        if self._controls.model is not None:
            body["agent"] = {"model": self._controls.model}
        if self._controls.spend_limit_usd_cents is not None:
            body["spend_control"] = {"limit": self._controls.spend_limit_usd_cents}
        if vaults is not None:
            body["vault_ids"] = [value for value in vaults]
        if initial is not None:
            if initial.mode != "new_turn":
                raise self._c.unsupported("initial_steer")
            body["input"] = [
                {"role": "user", "content": content(initial.content, self._c.profile_id)}
            ]
        return await self._decode(
            scope, await self._c.call("POST", "/agents/sessions", body=body, key=key)
        )

    @owned
    async def retrieve(self, scope: Scope, ref: ResourceRef) -> Session:
        self._c.check(scope, ref, "session")
        try:
            raw = await self._c.call("GET", "/agents/sessions/" + segment(ref.id))
        except ProviderError as error:
            if error.category != "not_found":
                raise
            binding = (
                self._binding_lookup(scope, ref.id) if self._binding_lookup is not None else None
            )
            raise ContinuityLost(
                binding.id if binding is not None else ref.id, ("provider session is missing",)
            ) from None
        if raw.get("id") != ref.id:
            raise ValueError("wrong session identity")
        return await self._decode(scope, raw)

    @owned
    async def list(
        self, scope: Scope, *, filters: SessionFilter, page: PageRequest
    ) -> Page[Session]:
        self._c.authorize(scope, "session")
        if not scope.is_platform:
            raise self._c.unsupported("tenant_session_listing")
        params = query(page)
        if filters.agent is not None:
            self._c.check(scope, filters.agent, "agent")
            params["agent_id"] = filters.agent.id
        if filters.created_after is not None or filters.include_archived:
            raise self._c.unsupported("session_filters")
        raw = await self._c.call("GET", "/agents/sessions", params=params)
        values = tuple([await self._decode(scope, item) for item in objects(raw["data"])])
        more = raw["has_more"]
        if not isinstance(more, bool):
            raise ValueError("invalid has_more")
        return Page(data=values, has_more=more, next_cursor=text(raw["last_id"]) if more else None)

    @owned
    async def plan_update(self, scope: Scope, ref: ResourceRef, desired: SessionSpec) -> UpdatePlan:
        self._c.check(scope, ref, "session")
        self._c.check(scope, desired.agent, "agent")
        if desired.environment is not None:
            self._c.check(scope, desired.environment, "environment")
        return UpdatePlan(
            session=ref,
            expected_revision=(await self.retrieve(scope, ref)).effective_revision,
            action="refuse",
            unmet=("session_conditional_update",),
        )

    @owned
    async def apply_update(
        self, scope: Scope, plan: UpdatePlan, *, expected: Revision, key: str
    ) -> UpdateReceipt:
        self._c.check(scope, plan.session, "session")
        raise self._c.unsupported("session_conditional_update")

    @owned
    async def archive(self, scope: Scope, ref: ResourceRef, *, key: str) -> Operation:
        self._c.check(scope, ref, "session")
        raise self._c.unsupported("session_archive")

    @owned
    async def delete(self, scope: Scope, ref: ResourceRef, *, key: str) -> DeletionReceipt:
        self._c.check(scope, ref, "session")
        path = "/agents/sessions/" + segment(ref.id)
        deleting = False
        try:
            async with asyncio.timeout(_DELETE_TIMEOUT):
                while True:
                    self._c.check(scope, ref, "session")
                    raw = await self._c.call("GET", path)
                    if raw.get("id") != ref.id:
                        raise ValueError("wrong session identity")
                    self._c.record(scope, raw)
                    vault_ids = raw.get("vault_ids", [])
                    if not isinstance(vault_ids, list):
                        raise ValueError("invalid vault IDs")
                    vaults = tuple(self._c.ref(scope, "vault", text(v)) for v in vault_ids)
                    status = text(raw["status"])
                    if status not in ("idle", "failed", "in_progress", "requires_action"):
                        raise ValueError("unknown native session status")
                    if status in ("idle", "failed"):
                        try:
                            deleting = True
                            await self._c.call("DELETE", path, key=key)
                        except ProviderError as error:
                            deleting = False
                            # A definite HTTP 409 rejects this deletion. An idle
                            # snapshot can race backend shutdown; re-read before
                            # another attempt, never replay an uncertain mutation.
                            if error.category != "conflict" or error.native_code != "409":
                                raise
                        else:
                            return DeletionReceipt(
                                operation_id=key, deleted=(ref,), retained=vaults
                            )
                    await asyncio.sleep(_DELETE_POLL_INTERVAL)
        except TimeoutError:
            if deleting:
                raise ProviderError(
                    "transient_network",
                    retryable=False,
                    native_code="session_delete_outcome_unknown",
                ) from None
            raise ProviderError(
                "conflict", retryable=True, native_code="session_delete_deadline"
            ) from None

    @owned
    async def export(
        self, scope: Scope, ref: ResourceRef, *, requested: ExportRequirements, key: str
    ) -> SessionExport:
        self._c.check(scope, ref, "session")
        raise self._c.unsupported("session_export")

    @owned
    async def restore(
        self,
        scope: Scope,
        export: SessionExport,
        target: SessionSpec,
        *,
        accept_losses: frozenset[str],
        key: str,
    ) -> RestoreReceipt:
        self._c.check(scope, export.source, "session")
        raise self._c.unsupported("session_restore")

    @owned
    async def migrate(
        self, scope: Scope, ref: ResourceRef, target: ConfigRevision, *, expected: int, key: str
    ) -> Session:
        self._c.check(scope, ref, "session")
        raise MigrationUnsupported(ref.id)
