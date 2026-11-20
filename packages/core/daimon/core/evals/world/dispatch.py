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

from daimon.core.evals.blobs import ObjectStore, object_key
from daimon.core.evals.format import (
    BlobRef,
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
    World,
    WorldCall,
    canonical_json,
    to_document,
)
from daimon.core.evals.world.matching import (
    clean,
    is_candidate,
    match_document,
    normalized_text,
    sim_key,
    static_match,
)
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
from daimon.core.evals.world.services.git import GitService, find_repo, pushes
from daimon.core.evals.world.services.github import GithubService
from daimon.core.evals.world.services.google import GoogleService
from daimon.core.evals.world.services.web import (
    FETCH_TOOL,
    SEARCH_TOOL,
    WebService,
    search_reply,
)
from daimon.core.evals.world.simulator import Check, SimContext, Simulator, simulate
from daimon.core.stores import eval_world, evals
from daimon.core.stores.eval_world import VERDICT_PREFIX
from daimon.core.stores.evals import BlobRow
from pydantic import JsonValue
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_HIDDEN_HEADERS: Final = frozenset({"authorization", "proxy-authorization", "cookie"})
_EARLIER: Final = 20
_CACHED_ITEMS: Final = 256
_TRIES: Final = 4
DAIMON_SERVER: Final = "daimon"


class ItemNotRunningError(Exception):
    """The item is unknown or not running, so nothing may answer for it."""


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


@dataclass(frozen=True)
class _Judge:
    """A `semantic` match only the model can decide; `key` names its cached verdict."""

    key: str
    service: str
    recorded: JsonValue
    live: JsonValue


@dataclass(frozen=True)
class _Simulate:
    key: str
    context: SimContext
    check: Check
    attempts: int


@dataclass
class _Work:
    """One call's model work so far, kept across its passes."""

    calls: int = 0
    written: str | None = None  # the sim key this call's answer was cached under
    failed: tuple[str, str] | None = None  # sim key and reason of an answer that failed


def _verdict_key(item: ItemRef, ref: str, live: JsonValue) -> str:
    document: JsonValue = {"case": item.case_sha256, "ref": ref, "live": clean(live)}
    return VERDICT_PREFIX + hashlib.sha256(canonical_json(document)).hexdigest()


def _search_query(request: WorldRequest) -> str | None:
    args = request.request.args
    query = args.get("query") if request.tool == SEARCH_TOOL and isinstance(args, dict) else None
    return query if isinstance(query, str) else None


def _blocked(world: World, request: WorldRequest) -> bool:
    """An HTTP host the case's network policy denies."""
    deny = world.network.deny if world.network else None
    if not deny or request.raw is None:
        return False
    host = request.raw.host
    return any(host == d.lower() or host.endswith(f".{d.lower()}") for d in deny)


@dataclass
class FakeWorld:
    sessions: async_sessionmaker[AsyncSession]
    simulator: Simulator | None
    git_root: Path
    services: tuple[Service, ...] = (GithubService(), GoogleService(), WebService())
    objects: ObjectStore | None = None
    _items: OrderedDict[uuid.UUID, ItemRef] = field(default_factory=OrderedDict[uuid.UUID, ItemRef])

    def __post_init__(self) -> None:
        self.git = GitService(self.git_root, self._load_blob)

    async def _put_blob(
        self, session: AsyncSession, tenant_id: uuid.UUID, row: BlobRow, content: bytes
    ) -> None:
        """As the eval service keeps blobs: in the artifact store when there is one."""
        if self.objects is not None:
            key = object_key(tenant_id, row.sha256)
            await self.objects.put(key=key, content=content, content_type=row.media_type)
        kept = None if self.objects is not None else content
        await evals.put_blob(session, tenant_id=tenant_id, blob=row, content=kept)

    async def _read_blob(
        self, session: AsyncSession, tenant_id: uuid.UUID, sha256: str
    ) -> bytes | None:
        content = await evals.blob_content(session, tenant_id=tenant_id, sha256=sha256)
        if content is None and self.objects is not None:
            content = await self.objects.read(key=object_key(tenant_id, sha256))
        return content

    async def _load_blob(self, item: ItemRef, sha256: str) -> bytes:
        async with self.sessions() as session:
            content = await self._read_blob(session, item.tenant_id, sha256)
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
        """Short locked passes over the item's state; model work runs between them, unlocked,
        and leaves its result in the cache the next pass reads."""
        work = _Work()
        for attempt in range(_TRIES):
            async with self.sessions() as session, session.begin():
                if not await eval_world.lock_running_item(session, item_id=item.id):
                    raise ItemNotRunningError(str(item.id))
                stats = await eval_world.call_stats(session, item_id=item.id)
                rows = await eval_world.list_overlay(session, item_id=item.id)
                overlay = Overlay(item.case.world.resources or (), rows)
                request = build(overlay)
                found = await self._answer(session, item, service, request, overlay, stats, work)
                if not isinstance(found, _Outcome) and attempt == _TRIES - 1:
                    found = _Outcome(_denied(request), "denied", "busy")
                if isinstance(found, _Outcome):
                    effects = self._effects(service, request, found)
                    await self._record(session, item, stats.next_seq, request, found, effects,
                                       overlay, model_calls=work.calls)  # fmt: skip
                    raw = await self._bytes(session, item, found.response, overlay)
                    return WorldAnswer(found.response, found.origin, effects, stats.next_seq,
                                       found.reason, raw)  # fmt: skip
            await self._think(item, found, work)
        raise AssertionError("the last pass always answers")

    async def _think(
        self, item: ItemRef, need: _Simulate | tuple[_Judge, ...], work: _Work
    ) -> None:
        """Model work outside any transaction; its result goes to the cache."""
        assert self.simulator is not None
        entries: list[tuple[str, str, eval_world.Document]] = []
        if isinstance(need, _Simulate):
            answer, made = await simulate(self.simulator, need.context, need.check, need.attempts)
            work.calls += made
            if isinstance(answer, str):
                work.failed = (need.key, f"invalid: {answer}"[:500])
                return
            entry = SimEntry(
                key=need.key, surface=need.context.request.surface,
                request=need.context.request.request, response=answer.response,
                model=answer.model, created_at=datetime.now(UTC),
            )  # fmt: skip
            entries.append((need.key, answer.model, to_document(entry)))
            work.written = need.key
        else:
            for judge in need:
                same = await self.simulator.same(item.id, judge.service, judge.recorded, judge.live)
                work.calls += 1
                entries.append((judge.key, "judge", {"same": same}))
                if same:
                    break
        async with self.sessions() as session, session.begin():
            for key, model, document in entries:
                await eval_world.put_sim(
                    session, tenant_id=item.tenant_id, case_sha256=item.case_sha256, key=key,
                    model=model, document=document,
                )  # fmt: skip

    async def _record(
        self,
        session: AsyncSession,
        item: ItemRef,
        seq: int,
        request: WorldRequest,
        outcome: _Outcome,
        effects: tuple[Effect, ...],
        overlay: Overlay | None = None,
        *,
        surface: Literal["mcp", "http", "provider_tool", "git"] | None = None,
        model_calls: int = 0,
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
            await self._put_blob(session, item.tenant_id, row, content)
        record = WorldCall(
            seq=seq, surface=surface or request.surface, service=request.service,
            server=request.server, tool=request.tool, request=request.request,
            response=outcome.response, origin=outcome.origin,
            exchange_id=outcome.exchange_id, sim_key=outcome.sim_key,
            effects=list(effects) or None, reason=outcome.reason,
        )  # fmt: skip
        await eval_world.record_call(
            session, item_id=item.id, tenant_id=item.tenant_id, document=to_document(record),
            model_calls=model_calls,
        )  # fmt: skip

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
        return await self._read_blob(session, item.tenant_id, sha) or b""

    @staticmethod
    async def _verdict(session: AsyncSession, item: ItemRef, judge: _Judge) -> bool | None:
        stored = await eval_world.get_sim(
            session, tenant_id=item.tenant_id, case_sha256=item.case_sha256, key=judge.key
        )
        same = stored.get("same") if stored else None
        return same if isinstance(same, bool) else None

    async def _answer(
        self,
        session: AsyncSession,
        item: ItemRef,
        service: Service,
        request: WorldRequest,
        overlay: Overlay,
        stats: eval_world.CallStats,
        work: _Work,
    ) -> _Outcome | _Simulate | tuple[_Judge, ...]:
        """The answer, or the model work it waits on: semantic verdicts, then a simulation."""
        world = item.case.world
        if stats.total >= (item.case.limits or Limits()).max_world_calls:
            return _Outcome(_denied(request), "denied", "max_world_calls")
        if _blocked(world, request):
            return _Outcome(_denied(request), "denied", "network_deny")
        settings = world.simulator or SimulatorSettings()
        thinking = self.simulator is not None and settings.enabled
        budget = settings.max_calls - stats.model_calls - work.calls if thinking else 0
        judges: list[_Judge] = []
        written = (res := service.resource(request)) is not None and overlay.written(
            service.name, *res
        )
        live = match_document(request.request, request.tool_info)
        for exchange in sorted(world.exchanges or (), key=lambda x: x.order or 0):
            if not is_candidate(exchange, request) or (written and exchange.kind == "read"):
                continue
            if not exchange.repeatable and exchange.id in stats.used_exchanges:
                continue
            found = static_match(exchange, request)
            if found is None:
                recorded = match_document(exchange.request, request.tool_info)
                judge = _Judge(_verdict_key(item, exchange.id, live),
                               exchange.service or request.service, recorded, live)  # fmt: skip
                found = await self._verdict(session, item, judge)
                if found is None and len(judges) < budget:
                    judges.append(judge)
            if found and judges:
                return tuple(judges)
            if found:
                service.remember(request, exchange.response, overlay)
                effects = tuple(exchange.effects) if exchange.effects is not None else None
                return _Outcome(exchange.response, "recorded", None, exchange.id, None, effects)
        if (query := _search_query(request)) is not None:
            for found_set in world.search or ():
                same: bool | None = normalized_text(found_set.query) == normalized_text(query)
                if not same and found_set.match == "semantic":
                    key = _verdict_key(item, f"search:{found_set.query}", query)
                    judge = _Judge(key, "web", found_set.query, query)
                    same = await self._verdict(session, item, judge)
                    if same is None and len(judges) < budget:
                        judges.append(judge)
                if same and not judges:
                    return _Outcome(search_reply(found_set), "recorded")
        if judges:
            return tuple(judges)
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
            reason = "written" if work.written == key else "cached"
            return _Outcome(cached.response, "simulated", reason, sim_key=key)
        if work.failed is not None and work.failed[0] == key:
            return _Outcome(_denied(request), "denied", work.failed[1], sim_key=key)
        if not thinking:
            return _Outcome(_denied(request), "denied", "simulation_off", sim_key=key)
        if budget < 1:
            return _Outcome(_denied(request), "denied", "simulator_max_calls", sim_key=key)
        earlier = await eval_world.list_calls(session, item_id=item.id, origin="simulated")
        context = SimContext(
            item.id, request, item.case, service.guidance(item, request), overlay.changes(),
            earlier[-_EARLIER:],
        )  # fmt: skip
        return _Simulate(key, context, lambda r: service.check(request, r), min(2, budget))

    async def _git(
        self, item: ItemRef, call: HttpCall, index: int, repo: Repo, service_path: str
    ) -> WorldAnswer:
        """Smart HTTP against the item's own copy of the repo, served unlocked; logged without
        pack data. A push's new state is kept in the overlay."""
        http = HttpRequest(method=call.method.upper(), url=call.url)
        request = WorldRequest("http", "git", ExchangeRequest(http=http), raw=call)
        async with self.sessions() as session, session.begin():
            if not await eval_world.lock_running_item(session, item_id=item.id):
                raise ItemNotRunningError(str(item.id))
            stats = await eval_world.call_stats(session, item_id=item.id)
            reason = (
                "max_world_calls"
                if stats.total >= (item.case.limits or Limits()).max_world_calls
                else "read_only"
                if pushes(service_path, call) and not repo.writable
                else None
            )
            if reason is not None:
                outcome = _Outcome(_denied(request), "denied", reason)
                await self._record(
                    session, item, stats.next_seq, request, outcome, (), surface="git"
                )
                return WorldAnswer(outcome.response, "denied", (), stats.next_seq, reason)
            rows = await eval_world.list_overlay(session, item_id=item.id)
        state = Overlay((), rows).get("git", "repo", str(index))
        bundle = BlobRef.model_validate(state["bundle"]) if state else repo.bundle
        reply = await self.git.serve(item, index, repo, bundle, service_path, call)
        response = ExchangeResponse(http=HttpResponse(status=reply.status, headers=reply.headers))
        outcome = _Outcome(response, "overlay")
        overlay = Overlay((), ())
        if reply.snapshot is not None:
            ref = overlay.put_blob(reply.snapshot, "application/x-git-bundle")
            overlay.put("git", "repo", str(index), {"bundle": to_document(ref)})
        async with self.sessions() as session, session.begin():
            if not await eval_world.lock_running_item(session, item_id=item.id):
                raise ItemNotRunningError(str(item.id))
            seq = (await eval_world.call_stats(session, item_id=item.id)).next_seq
            await self._record(
                session, item, seq, request, outcome, reply.effects, overlay, surface="git"
            )
        return WorldAnswer(response, "overlay", reply.effects, seq, None, reply.body)

    def forget(self, item_id: uuid.UUID) -> None:
        """Drop an ended item's local state (its repo copies); its records stay."""
        self._items.pop(item_id, None)
        self.git.forget(item_id)
