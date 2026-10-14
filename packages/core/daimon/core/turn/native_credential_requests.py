"""Operator-bound native credential offers; legacy request handlers stay separate.

The delivery edge registers the native request before posting its control. It
owns native interaction dispatch; it must never forward the control to an MA
resolver. This slice offers input only: no OAuth, consume, probe or expiry job.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, Protocol

from daimon.core.authz import (
    Action,
    Place,
    Subject,
    Surface,
    authorize,
    build_agent_ref,
)
from daimon.core.credential_requests import DEFAULT_TTL
from daimon.core.env_file import env_name_problem
from daimon.core.github_connect_cards import ConnectCard, build_connect_card
from daimon.core.neutral_resources import NeutralResourceHost
from daimon.core.posted_controls import CardButton, PostedCard, build_posted_card
from daimon.core.stores.access_policy import load_access_policy
from daimon.core.stores.accounts import get_account_with_tenant
from daimon.core.stores.domain import AccountIdentityRow, Role, TurnOriginRow
from daimon.core.stores.mux_state import PostgresStateStore
from daimon.core.stores.neutral_catalog import AgentRevision, CatalogConflict
from daimon.core.stores.turn_origins import get_active_origin
from mux.contracts.ids import ResourceRef, Scope, ThreadRef
from mux.contracts.profile import Profile
from mux.contracts.resources import ProviderBinding
from mux.errors import ScopeViolation, UnsupportedCapability
from mux.state.store import binding_slot
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


@dataclass(frozen=True)
class NativeCredentialOrigin:
    """Host-attested target and mux-session identity, checked against durable rows.

    The requester and the resource account are separate: A channel copy may
    belong to the guild resource account. No MA identity field certifies either.
    resource_scope comes from the authorized execution grant, independently of
    the returned host. session is a scoped mux session, not a provider Session API
    identity; production wiring must attest its binding before enabling offers.
    """

    origin_id: uuid.UUID
    requester_account_id: uuid.UUID
    binding: ProviderBinding
    session: ResourceRef
    agent: ResourceRef
    catalog_id: str
    catalog_revision: int
    resource_scope: Scope


@dataclass(frozen=True)
class NativeCredentialRequest:
    ref: ResourceRef
    origin: NativeCredentialOrigin
    thread: ThreadRef
    requester_platform_user_id: str
    kind: Literal["github", "env"]
    target: str
    agent_name: str
    expires_at: datetime

    @property
    def custom_id(self) -> str:
        return f"nc_{self.ref.provider}_input:" + self.ref.id


@dataclass(frozen=True)
class NativeOfferReceipt:
    request: ResourceRef
    thread: ThreadRef
    message_id: str


class NativeCredentialDelivery(Protocol):
    """Explicit native registry and platform delivery; no legacy fallback.

    register must durably retain the full request and install a native handler
    before acknowledging. post returns the platform's actual message identity.
    Neither method may consume/probe credentials or begin OAuth in this slice.
    """

    async def register(self, request: NativeCredentialRequest) -> None: ...

    async def post(
        self, request: NativeCredentialRequest, card: ConnectCard | PostedCard
    ) -> NativeOfferReceipt: ...


class NativeCredentialAdapter:
    """Explicit scoped product edge. No existing runtime enables it by default."""

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        host: NeutralResourceHost,
        *,
        profile: Profile,
        operator_enabled: bool = False,
        delivery: NativeCredentialDelivery | None = None,
        public_base_url: str,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.profile = profile
        self.sessions = sessions
        self.host = host
        self.operator_enabled = operator_enabled
        self.delivery = delivery
        self.public_base_url = public_base_url
        self.now = now
        self.store = PostgresStateStore(sessions)

    def _enabled(self) -> NativeCredentialDelivery:
        if self.operator_enabled is not True or self.delivery is None:
            raise UnsupportedCapability(
                (f"{self.profile.provider}_native_credential_delivery",),
                self.profile.profile_id,
            )
        capabilities = self.host.backend.capabilities()
        if (
            capabilities.profile_id != self.profile.profile_id
            or capabilities.provider != self.profile.provider
        ):
            raise UnsupportedCapability(
                (f"{self.profile.provider}_native_credential_profile",),
                self.profile.profile_id,
            )
        return self.delivery

    def _route(self, origin: NativeCredentialOrigin) -> None:
        scope = self.host.scope
        binding, agent, session = origin.binding, origin.agent, origin.session
        if (
            scope != origin.resource_scope
            or scope.is_platform
            or scope.is_legacy_host_authorized
            or scope.principal_id != f"mcp:{origin.requester_account_id}"
            or not scope.authorization_id.strip()
            or binding.provider != self.profile.provider
            or binding.profile != self.profile.profile_id
            or binding.thread.channel.tenant_id != scope.tenant_id
            or binding.thread.channel.platform != "discord"
            or binding.legacy_account_id != str(origin.requester_account_id)
            or not origin.catalog_id.strip()
            or origin.catalog_revision < 1
            or agent.provider != self.profile.provider
            or agent.kind != "agent"
            or agent.tenant_id != scope.tenant_id
            or agent.account_id != scope.account_id
            or agent.account_scope_id != self.host.workspace
            or session.provider != self.profile.provider
            or session.kind != "session"
            or session.tenant_id != scope.tenant_id
            or session.account_id != str(origin.requester_account_id)
            or session.account_scope_id != self.host.workspace
            or binding.native_refs.get("session") != session.id
            or binding.native_refs.get("agent") != agent.id
            or binding.native_refs.get("account_scope") != self.host.workspace
        ):
            raise ScopeViolation(origin.agent.id, "native credential origin is outside its grant")

    async def _verify(
        self, origin: NativeCredentialOrigin
    ) -> tuple[TurnOriginRow, AccountIdentityRow, AgentRevision]:
        self._route(origin)
        async with self.sessions() as db:
            place = await get_active_origin(
                db,
                origin_id=origin.origin_id,
                tenant_id=uuid.UUID(self.host.scope.tenant_id),
                account_id=origin.requester_account_id,
                platform="discord",
                now=self.now(),
            )
            actor = await get_account_with_tenant(db, account_id=origin.requester_account_id)
            if (
                place is None
                or actor is None
                or place.is_external
                or actor.is_external
                or actor.tenant_id != place.tenant_id
                or actor.platform != place.platform
                or not actor.platform_user_id
                or place.parent_channel_id != origin.binding.thread.channel.channel_id
                or place.thread_id != origin.binding.thread.thread_id
            ):
                raise ScopeViolation(
                    str(origin.origin_id), "native credential caller/place expired"
                )
            policy = await load_access_policy(db, tenant_id=place.tenant_id)
        if await self.store.get_binding(binding_slot(origin.binding)) != origin.binding:
            raise ScopeViolation(origin.binding.id, "native credential binding changed")
        revision = await self.store.get_config_revision(
            origin.binding.thread.channel, origin.binding.config_revision
        )
        if (
            revision is None
            or revision.profile != self.profile.profile_id
            or revision.backend != self.profile.provider
            or revision.thread_mode != "per_caller"
        ):
            raise ScopeViolation(origin.binding.id, "native credential configuration unavailable")
        target = await self.host.entry(origin.catalog_id)
        if (
            target.local_revision != origin.catalog_revision
            or target.agent.ref != origin.agent
            or target.agent.spec.model.provider != self.profile.provider
            or target.agent.spec.model.id != revision.model
        ):
            raise CatalogConflict("native credential target changed")
        subject = Subject(
            is_admin=actor.role is Role.ADMIN, platform_user_id=actor.platform_user_id
        )
        agent = build_agent_ref(target.agent.spec.name, target.agent.spec.metadata or {})
        location = Place.from_origin(
            parent_channel_id=place.parent_channel_id, thread_id=place.thread_id
        )
        for action in (Action.CONFIGURE, Action.POST):
            if not authorize(
                policy,
                subject=subject,
                action=action,
                surface=Surface.CONFIG,
                agent=agent,
                place=location,
            ):
                raise ScopeViolation(origin.agent.id, "native credential policy refuses this offer")
        return place, actor, target

    async def offer(
        self,
        origin: NativeCredentialOrigin,
        *,
        kind: Literal["github", "env"],
        target: str = "GitHub",
    ) -> NativeOfferReceipt:
        delivery = self._enabled()
        if kind not in ("github", "env"):
            raise ValueError("this native slice offers GitHub or an environment key only")
        _, actor, row = await self._verify(origin)
        self._request_policy(kind, target, actor)
        # Actual native readback is independent of the catalog/binding evidence.
        readback = await self.host.read(origin.catalog_id)
        if readback != row:
            raise CatalogConflict("native target changed during readback")
        _, actor, row = await self._verify(origin)
        self._request_policy(kind, target, actor)
        request = NativeCredentialRequest(
            ref=ResourceRef(
                id=uuid.uuid4().hex,
                kind="credential_request",
                provider=self.profile.provider,
                account_scope_id=self.host.workspace,
                tenant_id=self.host.scope.tenant_id,
                account_id=self.host.scope.account_id,
            ),
            origin=origin,
            thread=origin.binding.thread,
            requester_platform_user_id=actor.platform_user_id or "",
            kind=kind,
            target=target,
            agent_name=row.agent.spec.name,
            expires_at=self.now() + DEFAULT_TTL,
        )
        await delivery.register(request)
        # Registration can suspend or lose the binding/origin; never post stale controls.
        _, actor, _ = await self._verify(origin)
        self._request_policy(kind, target, actor)
        if kind == "github":
            card: ConnectCard | PostedCard = build_connect_card(
                agent_name=request.agent_name,
                identity_enabled=False,
                avatar_url=None,
                public_base_url=self.public_base_url,
            )
        else:
            shared = build_posted_card(
                kind="env",
                state="requested",
                agent_name=request.agent_name,
                responder_name=request.agent_name,
                target=target,
                requester_platform_user_id=request.requester_platform_user_id,
                expires_at=request.expires_at,
                token=request.ref.id,
            )
            card = shared.model_copy(
                update={
                    "buttons": tuple(
                        CardButton(
                            label=button.label,
                            style="primary",
                            custom_id=request.custom_id,
                        )
                        for button in shared.buttons
                    )
                }
            )
        receipt = await delivery.post(request, card)
        if (
            receipt.request != request.ref
            or receipt.thread != request.thread
            or not receipt.message_id.strip()
        ):
            raise ScopeViolation(request.ref.id, "native credential delivery receipt mismatched")
        return receipt

    @staticmethod
    def _request_policy(
        kind: Literal["github", "env"], target: str, actor: AccountIdentityRow
    ) -> None:
        if kind == "github" and (actor.role is not Role.ADMIN or target != "GitHub"):
            raise ScopeViolation(target, "native GitHub offers require a current admin")
        if kind == "env" and env_name_problem(target, is_admin=actor.role is Role.ADMIN):
            raise ValueError("that credential key name cannot be requested")
