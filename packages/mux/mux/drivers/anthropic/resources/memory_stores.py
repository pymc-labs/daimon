"""Native memory stores and reads; the SDK adds its agent-memory beta header."""

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Literal, Protocol, cast

from anthropic import AsyncAnthropic
from anthropic.types.beta import BetaManagedAgentsMemoryStore
from anthropic.types.beta.memory_store_list_params import MemoryStoreListParams
from anthropic.types.beta.memory_stores.beta_managed_agents_memory import BetaManagedAgentsMemory
from anthropic.types.beta.memory_stores.beta_managed_agents_memory_list_item import (
    BetaManagedAgentsMemoryListItem,
)
from anthropic.types.beta.memory_stores.memory_list_params import MemoryListParams
from pydantic import JsonValue

from mux.contracts.ids import Page, PageRequest, ResourceRef, Revision, Scope
from mux.contracts.ports import MemoryStores as CoreMemoryStores
from mux.contracts.receipts import DeletionReceipt, Operation
from mux.contracts.resources import Memory, MemoryStore
from mux.drivers.anthropic.resources._authorization import (
    ResourceAuthorization,
    authorize,
    check_record,
    check_ref,
    visible,
    visible_grant,
)
from mux.drivers.anthropic.resources._errors import provider_call, provider_iter
from mux.drivers.anthropic.resources._native import NativeSnapshot, native_snapshot
from mux.drivers.anthropic.resources.vaults import operation
from mux.drivers.anthropic.schemas import NativeConfig
from mux.errors import UnsupportedCapability


class StoreCreate(NativeConfig):
    name: str
    description: str | None = None
    metadata: dict[str, str] | None = None


class MemoryPath(NativeConfig):
    type: Literal["memory"]
    path: str
    id: str


class MemoryPrefix(NativeConfig):
    type: Literal["memory_prefix"]
    path: str
    id: None = None


type MemoryEntry = MemoryPath | MemoryPrefix


class MemoryStores(CoreMemoryStores, Protocol):
    async def create_native(
        self, scope: Scope, config: StoreCreate, *, key: str
    ) -> MemoryStore: ...
    def walk(
        self, scope: Scope, store: ResourceRef, *, path_prefix: str
    ) -> AsyncIterator[MemoryEntry]: ...
    def walk_native(
        self, scope: Scope, store: ResourceRef, *, path_prefix: str
    ) -> AsyncIterator[NativeSnapshot]: ...
    async def read_native(
        self, scope: Scope, store: ResourceRef, memory_id: str
    ) -> NativeSnapshot: ...


class AnthropicMemoryStores:
    def __init__(
        self,
        client: AsyncAnthropic,
        account_scope_id: str,
        authorization: ResourceAuthorization | None = None,
    ) -> None:
        self._client = client
        self._account_scope_id = account_scope_id
        self._authorization = authorization

    def _check(self, scope: Scope, store: ResourceRef) -> None:
        authorize(self._authorization, scope, "memory_store", store.id)
        check_ref(scope, store, self._account_scope_id, "memory_store")

    def _store(self, scope: Scope, item: object, *, name: str = "") -> MemoryStore:
        if not isinstance(item, BetaManagedAgentsMemoryStore):
            # Existing create callers use only the returned opaque id.
            item = BetaManagedAgentsMemoryStore.model_construct(**vars(item))
        check_record(scope, item.id, item.metadata)
        return MemoryStore(
            ref=ResourceRef(
                id=item.id,
                kind="memory_store",
                provider="anthropic",
                account_scope_id=self._account_scope_id,
                tenant_id=scope.tenant_id,
                account_id=scope.account_id,
            ),
            name=getattr(item, "name", None) or name,
            description=item.description,
            created_at=getattr(item, "created_at", None) or datetime(1970, 1, 1, tzinfo=UTC),
            updated_at=item.updated_at,
            archived_at=item.archived_at,
            native=cast(JsonValue, item.model_dump(mode="json", exclude_unset=True)),
        )

    def _memory(self, item: BetaManagedAgentsMemory) -> Memory:
        return Memory(
            id=item.id,
            path=item.path,
            content=item.content or "",
            revision=Revision(local=0, native=item.memory_version_id),
        )

    async def retrieve(self, scope: Scope, store: ResourceRef) -> MemoryStore:
        self._check(scope, store)
        return self._store(
            scope, await provider_call(self._client.beta.memory_stores.retrieve(store.id))
        )

    async def list(self, scope: Scope, *, page: PageRequest) -> Page[MemoryStore]:
        authorize(self._authorization, scope, "memory_store")
        kwargs: MemoryStoreListParams = {}
        if page.limit is not None:
            kwargs["limit"] = page.limit
        if page.cursor is not None:
            kwargs["page"] = page.cursor
        if page.order is not None:
            raise UnsupportedCapability(("memory_store_list_order",), "anthropic.managed_agents")
        response = await provider_call(self._client.beta.memory_stores.list(**kwargs))
        return Page(
            data=tuple(
                self._store(scope, item)
                for item in (response.data or ())
                if visible(scope, item.metadata)
                and visible_grant(self._authorization, scope, "memory_store", item.id)
            ),
            has_more=bool(response.next_page),
            next_cursor=response.next_page or None,
        )

    async def create_native(self, scope: Scope, config: StoreCreate, *, key: str) -> MemoryStore:
        from anthropic.types.beta.memory_store_create_params import MemoryStoreCreateParams

        authorize(self._authorization, scope, "memory_store")
        check_record(scope, "new-memory-store", config.metadata)
        item = await provider_call(
            self._client.beta.memory_stores.create(
                **cast(MemoryStoreCreateParams, config.model_dump(exclude_unset=True))
            )
        )
        return self._store(scope, item, name=config.name)

    async def create(self, scope: Scope, name: str, description: str, *, key: str) -> ResourceRef:
        return (
            await self.create_native(
                scope, StoreCreate(name=name, description=description), key=key
            )
        ).ref

    async def _walk_sdk(
        self, scope: Scope, store: ResourceRef, *, path_prefix: str
    ) -> AsyncIterator[BetaManagedAgentsMemoryListItem]:
        self._check(scope, store)
        page = await provider_call(
            self._client.beta.memory_stores.memories.list(store.id, path_prefix=path_prefix)
        )
        async for item in provider_iter(page):
            yield item

    async def walk_native(
        self, scope: Scope, store: ResourceRef, *, path_prefix: str
    ) -> AsyncIterator[NativeSnapshot]:
        # Legacy consumers ignore prefix/unknown rows and may not use memory ids.
        # Preserve the parser's partial records until the caller selects a field.
        async for item in self._walk_sdk(scope, store, path_prefix=path_prefix):
            yield native_snapshot(item)

    async def walk(
        self, scope: Scope, store: ResourceRef, *, path_prefix: str
    ) -> AsyncIterator[MemoryEntry]:
        async for item in self._walk_sdk(scope, store, path_prefix=path_prefix):
            yield (
                MemoryPath(type="memory", path=item.path, id=item.id)
                if item.type == "memory"
                else MemoryPrefix(type="memory_prefix", path=item.path)
            )

    async def memories(
        self, scope: Scope, store: ResourceRef, *, path_prefix: str, page: PageRequest
    ) -> Page[Memory]:
        self._check(scope, store)
        kwargs: MemoryListParams = {"path_prefix": path_prefix}
        if page.limit is not None:
            kwargs["limit"] = page.limit
        if page.cursor is not None:
            kwargs["page"] = page.cursor
        if page.order is not None:
            raise UnsupportedCapability(("memory_list_order",), "anthropic.managed_agents")
        response = await provider_call(
            self._client.beta.memory_stores.memories.list(store.id, **kwargs)
        )
        return Page(
            data=tuple(
                self._memory(item) for item in (response.data or ()) if item.type == "memory"
            ),
            has_more=bool(response.next_page),
            next_cursor=response.next_page or None,
        )

    async def _read_sdk(
        self, scope: Scope, store: ResourceRef, memory_id: str
    ) -> BetaManagedAgentsMemory:
        self._check(scope, store)
        return await provider_call(
            self._client.beta.memory_stores.memories.retrieve(
                memory_id, memory_store_id=store.id, view="full"
            )
        )

    async def read_native(self, scope: Scope, store: ResourceRef, memory_id: str) -> NativeSnapshot:
        # memory_view consumes only content; identity/path may be absent.
        return native_snapshot(await self._read_sdk(scope, store, memory_id))

    async def read(self, scope: Scope, store: ResourceRef, memory_id: str) -> Memory:
        return self._memory(await self._read_sdk(scope, store, memory_id))

    async def write(
        self,
        scope: Scope,
        store: ResourceRef,
        path: str,
        content: str,
        *,
        expected: Revision | None,
        key: str,
    ) -> Memory:
        self._check(scope, store)
        if expected is not None:
            raise UnsupportedCapability(("memory_update_by_path",), "anthropic.managed_agents")
        return self._memory(
            await provider_call(
                self._client.beta.memory_stores.memories.create(
                    store.id, path=path, content=content, view="full"
                )
            )
        )

    async def archive(self, scope: Scope, store: ResourceRef, *, key: str) -> Operation:
        self._check(scope, store)
        await provider_call(self._client.beta.memory_stores.archive(store.id))
        return operation(store, key, "archive_memory_store")

    async def delete(self, scope: Scope, store: ResourceRef, *, key: str) -> DeletionReceipt:
        self._check(scope, store)
        await provider_call(self._client.beta.memory_stores.delete(store.id))
        return DeletionReceipt(operation_id=key, deleted=(store,))
