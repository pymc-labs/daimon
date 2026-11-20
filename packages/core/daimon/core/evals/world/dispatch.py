"""`FakeWorld`: every call an eval item makes, answered in a fixed order (design section 8).

1. A recorded exchange that matches (a non-repeatable one answers once).
2. The item's overlay, through the service that plays the host.
3. A cached simulator answer: the case's `sim_cache`, then the deployment's cache table.
4. The simulator, checked and retried once.
5. Denied: simulation off, over `simulator.max_calls`, over `limits.max_world_calls`, or an
   answer that failed its checks twice.

The item's row lock orders its calls; each call, its overlay writes and new blobs commit
together, and every call is logged as a `WorldCall`.
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, Literal

from daimon.core.evals.format import (
    Body,
    Case,
    ContentBlock,
    Effect,
    ExchangeRequest,
    ExchangeResponse,
    HttpRequest,
    HttpResponse,
    JsonObject,
    Limits,
    McpTool,
    Origin,
    Repo,
    SimEntry,
    SimulatorSettings,
    WorldCall,
    to_document,
)
from daimon.core.evals.world.matching import is_candidate, matches, sim_key
from daimon.core.evals.world.model import (
    HttpCall,
    ItemRef,
    Service,
    WorldAnswer,
    WorldRequest,
    body_bytes,
    body_of,
)
from daimon.core.evals.world.overlay import Overlay
from daimon.core.evals.world.services.generic import GenericService
from daimon.core.evals.world.services.git import GitService, find_repo
from daimon.core.evals.world.services.github import GithubService
from daimon.core.evals.world.services.google import GoogleService
from daimon.core.evals.world.services.web import (
    FETCH_TOOL,
    SEARCH_TOOL,
    WebService,
    search_reply,
    search_set,
)
from daimon.core.evals.world.simulator import SimContext, Simulator, simulate
from daimon.core.stores import eval_world, evals
from daimon.core.stores.evals import BlobRow
from pydantic import JsonValue
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

#: An MCP token for an eval item carries this label prefix and the item id.
EVAL_TOKEN_LABEL_PREFIX: Final = "eval-item:"
_HIDDEN_HEADERS: Final = frozenset({"authorization", "proxy-authorization", "cookie"})
_EARLIER: Final = 20
_CACHED_ITEMS: Final = 256
DAIMON_SERVER: Final = "daimon"


class ItemNotRunningError(Exception):
    """The item is unknown or not running, so nothing may answer for it."""


def eval_token_label(item_id: uuid.UUID) -> str:
    return f"{EVAL_TOKEN_LABEL_PREFIX}{item_id}"


def item_id_from_label(label: str | None) -> uuid.UUID | None:
    if not label or not label.startswith(EVAL_TOKEN_LABEL_PREFIX):
        return None
    try:
        return uuid.UUID(label.removeprefix(EVAL_TOKEN_LABEL_PREFIX))
    except ValueError:
        return None


def _token_sha256(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


async def issue_world_token(session: AsyncSession, item_id: uuid.UUID) -> str:
    """A new bearer token for the item's gateway and eval MCP servers; the old one stops."""
    token = secrets.token_urlsafe(32)
    await eval_world.set_world_token(session, item_id=item_id, token_sha256=_token_sha256(token))
    return token


def _denied(request: WorldRequest) -> ExchangeResponse:
    if request.surface == "http":
        return ExchangeResponse(http=HttpResponse(status=403))
    text = "This request could not be completed."
    return ExchangeResponse(is_error=True, content=[ContentBlock(type="text", text=text)])


@dataclass(frozen=True)
class _Outcome:
    response: ExchangeResponse
    origin: Origin
    reason: str | None = None
    exchange_id: str | None = None
    sim_key: str | None = None
    effects: tuple[Effect, ...] | None = None  # None: the service's own


async def _never_same(_service: str, _recorded: JsonValue, _live: JsonValue) -> bool:
    return False


@dataclass
class FakeWorld:
    sessions: async_sessionmaker[AsyncSession]
    simulator: Simulator | None
    git_root: Path
    services: tuple[Service, ...] = (GithubService(), GoogleService(), WebService())
    _items: OrderedDict[uuid.UUID, ItemRef] = field(default_factory=OrderedDict[uuid.UUID, ItemRef])

    def __post_init__(self) -> None:
        self.git = GitService(self.git_root, self._load_blob)

    async def _load_blob(self, item: ItemRef, sha256: str) -> bytes:
        async with self.sessions() as session:
            content = await evals.blob_content(session, tenant_id=item.tenant_id, sha256=sha256)
        if content is None:
            raise ItemNotRunningError(f"blob {sha256} of item {item.id} is missing")
        return content

    def _ref(self, row: eval_world.WorldItemRow) -> ItemRef:
        cached = self._items.get(row.id)
        if cached is None or cached.case_sha256 != row.case_sha256:
            cached = ItemRef(row.id, row.tenant_id, Case.model_validate(row.case_document),
                             row.case_sha256)  # fmt: skip
            self._items[row.id] = cached
            while len(self._items) > _CACHED_ITEMS:
                self._items.popitem(last=False)
        return cached

    async def item(self, item_id: uuid.UUID) -> ItemRef | None:
        """The running item, or None."""
        async with self.sessions() as session:
            row = await eval_world.running_item(session, item_id=item_id)
        return self._ref(row) if row else None

    async def item_for_token(self, token: str) -> ItemRef | None:
        async with self.sessions() as session:
            row = await eval_world.running_item(session, token_sha256=_token_sha256(token))
        return self._ref(row) if row else None

    # -- entry points --------------------------------------------------------------------

    async def http(self, item: ItemRef, call: HttpCall) -> WorldAnswer:
        """An HTTP request the item's sandbox made; `WorldAnswer.raw_body` holds the bytes."""
        if (found := find_repo(item, call)) is not None:
            return await self._git(item, call, *found)
        service = next((s for s in self.services if s.claims(item, call)), GenericService())

        def build(overlay: Overlay) -> WorldRequest:
            headers = {k: v for k, v in call.headers.items() if k.lower() not in _HIDDEN_HEADERS}
            kind = call.header("content-type")
            body = body_of(call.body, kind)
            if body is None and call.body:
                blob = overlay.put_blob(call.body, kind or "application/octet-stream")
                body = Body(blob=blob, media_type=blob.media_type)
            http = HttpRequest(method=call.method.upper(), url=call.url, headers=headers, body=body)
            return WorldRequest("http", service.name, ExchangeRequest(http=http), raw=call)

        return await self._dispatch(item, service, build)

    async def mcp(
        self,
        item: ItemRef,
        server: str,
        tool: str,
        args: JsonObject,
        tool_info: McpTool | None = None,
    ) -> WorldAnswer:
        web = server == DAIMON_SERVER and tool in (FETCH_TOOL, SEARCH_TOOL)
        service: Service = WebService() if web else GenericService()
        name = "web" if web else server
        request = WorldRequest("mcp", name, ExchangeRequest(args=args), server, tool, tool_info)
        return await self._dispatch(item, service, lambda _overlay: request)

    async def provider_tool(self, item: ItemRef, tool: str, args: JsonObject) -> WorldAnswer:
        web = tool in (FETCH_TOOL, SEARCH_TOOL)
        service: Service = WebService() if web else GenericService()
        name = "web" if web else "provider"
        request = WorldRequest("provider_tool", name, ExchangeRequest(args=args), tool=tool)
        return await self._dispatch(item, service, lambda _overlay: request)

    # -- dispatch ------------------------------------------------------------------------

    async def _dispatch(
        self, item: ItemRef, service: Service, build: Callable[[Overlay], WorldRequest]
    ) -> WorldAnswer:
        async with self.sessions() as session, session.begin():
            if not await eval_world.lock_running_item(session, item_id=item.id):
                raise ItemNotRunningError(str(item.id))
            stats = await eval_world.call_stats(session, item_id=item.id)
            rows = await eval_world.list_overlay(session, item_id=item.id)
            overlay = Overlay(item.case.world.resources or (), rows)
            request = build(overlay)
            if stats.total >= (item.case.limits or Limits()).max_world_calls:
                outcome = _Outcome(_denied(request), "denied", "max_world_calls")
            else:
                outcome = await self._answer(session, item, service, request, overlay, stats)
            effects = self._effects(service, request, outcome)
            await self._record(session, item, stats.next_seq, request, outcome, effects, overlay)
            raw = await self._bytes(session, item, outcome.response, overlay)
        return WorldAnswer(outcome.response, outcome.origin, effects, stats.next_seq,
                           outcome.reason, raw)  # fmt: skip

    @staticmethod
    async def _record(
        session: AsyncSession,
        item: ItemRef,
        seq: int,
        request: WorldRequest,
        outcome: _Outcome,
        effects: tuple[Effect, ...],
        overlay: Overlay | None = None,
        *,
        surface: Literal["mcp", "http", "provider_tool", "git"] | None = None,
    ) -> None:
        """Persist the call's overlay writes and new blobs, and log the call."""
        for (svc, key), data in overlay.writes.items() if overlay else ():
            await eval_world.upsert_overlay(
                session, item_id=item.id, tenant_id=item.tenant_id, service=svc,
                resource_key=key, data=data,
            )  # fmt: skip
        for ref, content in overlay.blobs.values() if overlay else ():
            row = BlobRow(sha256=ref.sha256, size=ref.size, media_type=ref.media_type,
                          scrub="generated", needs_human_check=False)  # fmt: skip
            await evals.put_blob(session, tenant_id=item.tenant_id, blob=row, content=content)
        record = WorldCall(
            seq=seq, surface=surface or request.surface, service=request.service,
            server=request.server, tool=request.tool, request=request.request,
            response=outcome.response, origin=outcome.origin,
            exchange_id=outcome.exchange_id, sim_key=outcome.sim_key,
            effects=list(effects) or None, reason=outcome.reason,
        )  # fmt: skip
        await eval_world.record_call(
            session, item_id=item.id, tenant_id=item.tenant_id, document=to_document(record)
        )

    @staticmethod
    def _effects(service: Service, request: WorldRequest, outcome: _Outcome) -> tuple[Effect, ...]:
        if outcome.origin == "denied":
            return ()
        if outcome.effects is not None:
            return outcome.effects
        found = service.effects(request)
        if not found and request.server == DAIMON_SERVER and request.tool:
            return (Effect(verb=f"daimon.{request.tool}.call"),)
        return found

    async def _bytes(
        self, session: AsyncSession, item: ItemRef, response: ExchangeResponse, overlay: Overlay
    ) -> bytes:
        body = response.http.body if response.http else None
        raw = body_bytes(body)
        if raw is not None or body is None or body.blob is None:
            return raw or b""
        sha = body.blob.sha256
        if sha in overlay.blobs:
            return overlay.blobs[sha][1]
        return await evals.blob_content(session, tenant_id=item.tenant_id, sha256=sha) or b""

    async def _answer(
        self,
        session: AsyncSession,
        item: ItemRef,
        service: Service,
        request: WorldRequest,
        overlay: Overlay,
        stats: eval_world.CallStats,
    ) -> _Outcome:
        same = self.simulator.same if self.simulator else _never_same
        world = item.case.world
        exchanges = sorted(world.exchanges or (), key=lambda x: x.order or 0)
        for exchange in exchanges:
            if not is_candidate(exchange, request):
                continue
            if not exchange.repeatable and exchange.id in stats.used_exchanges:
                continue
            if await matches(exchange, request, same):
                service.remember(request, exchange.response, overlay)
                effects = tuple(exchange.effects) if exchange.effects is not None else None
                return _Outcome(exchange.response, "recorded", None, exchange.id, None, effects)
        if request.tool == SEARCH_TOOL and isinstance(request.request.args, dict):
            query = request.request.args.get("query")
            if isinstance(query, str) and (found := await search_set(item, query, same)):
                return _Outcome(search_reply(found), "recorded")
        if (shaped := service.answer(item, request, overlay)) is not None:
            return _Outcome(shaped, "overlay")
        key = sim_key(request, overlay.digest())
        cached = next((e for e in world.sim_cache or () if e.key == key), None)
        if cached is None and (
            stored := await eval_world.get_sim(
                session, tenant_id=item.tenant_id, case_sha256=item.case_sha256, key=key
            )
        ):
            cached = SimEntry.model_validate(stored)
        if cached is not None:
            service.remember(request, cached.response, overlay)
            return _Outcome(cached.response, "simulated", "cached", sim_key=key)
        settings = world.simulator or SimulatorSettings()
        if self.simulator is None or not settings.enabled:
            return _Outcome(_denied(request), "denied", "simulation_off", sim_key=key)
        if stats.written >= settings.max_calls:
            return _Outcome(_denied(request), "denied", "simulator_max_calls", sim_key=key)
        earlier = await eval_world.list_calls(session, item_id=item.id, origin="simulated")
        context = SimContext(
            item.id, request, item.case, service.guidance(item, request), overlay.changes(),
            earlier[-_EARLIER:],
        )  # fmt: skip
        answer = await simulate(self.simulator, context, lambda r: service.check(request, r))
        if isinstance(answer, str):
            return _Outcome(_denied(request), "denied", f"invalid: {answer}"[:500], sim_key=key)
        entry = SimEntry(
            key=key, surface=request.surface, request=request.request,
            response=answer.response, model=answer.model, created_at=datetime.now(UTC),
        )  # fmt: skip
        await eval_world.put_sim(
            session, tenant_id=item.tenant_id, case_sha256=item.case_sha256, key=key,
            model=answer.model, document=to_document(entry),
        )  # fmt: skip
        service.remember(request, answer.response, overlay)
        return _Outcome(answer.response, "simulated", "written", sim_key=key)

    async def _git(
        self, item: ItemRef, call: HttpCall, repo: Repo, service_path: str
    ) -> WorldAnswer:
        """Smart HTTP against the item's own copy of the repo; logged without pack data."""
        http = HttpRequest(method=call.method.upper(), url=call.url)
        request = WorldRequest("http", "git", ExchangeRequest(http=http), raw=call)
        async with self.sessions() as session, session.begin():
            if not await eval_world.lock_running_item(session, item_id=item.id):
                raise ItemNotRunningError(str(item.id))
            stats = await eval_world.call_stats(session, item_id=item.id)
            if stats.total >= (item.case.limits or Limits()).max_world_calls:
                outcome = _Outcome(_denied(request), "denied", "max_world_calls")
                await self._record(
                    session, item, stats.next_seq, request, outcome, (), surface="git"
                )
                return WorldAnswer(outcome.response, "denied", (), stats.next_seq, outcome.reason)
            reply = await self.git.serve(item, repo, service_path, call)
            http_reply = HttpResponse(status=reply.status, headers=reply.headers)
            response = ExchangeResponse(http=http_reply)
            outcome = _Outcome(response, "overlay")
            await self._record(
                session, item, stats.next_seq, request, outcome, reply.effects, surface="git"
            )
        return WorldAnswer(response, "overlay", reply.effects, stats.next_seq, None, reply.body)
