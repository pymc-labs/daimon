"""Scope and resource codecs, independent of all other provider drivers."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime
from functools import wraps
from types import CoroutineType
from typing import Any

from pydantic import JsonValue

from mux.contracts.ids import Page, PageRequest, ResourceRef, Revision, Scope
from mux.drivers.openai.transport import Object, Transport, object_json
from mux.errors import ProviderError, ScopeViolation, UnsupportedCapability

Authorization = Callable[[Scope, str, str | None], bool]


def text(value: JsonValue) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("expected nonempty string")
    return value


def objects(value: JsonValue) -> tuple[Object, ...]:
    if not isinstance(value, list):
        raise ValueError("expected list")
    return tuple(object_json(v) for v in value)


def timestamp(value: JsonValue) -> datetime:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError("expected Unix timestamp")
    return datetime.fromtimestamp(value, UTC)


def revision(value: Mapping[str, JsonValue]) -> Revision:
    # Native resources expose no version/precondition. This fingerprint is
    # descriptive only; update ports refuse to pretend it is a provider CAS.
    encoded = json.dumps(dict(value), sort_keys=True, separators=(",", ":")).encode()
    return Revision(local=0, native=hashlib.sha256(encoded).hexdigest())


def query(page: PageRequest) -> dict[str, str | int]:
    if page.limit is not None and page.limit > 100:
        raise ProviderError("invalid_request", retryable=False, native_code="page_limit")
    result: dict[str, str | int] = {}
    if page.cursor is not None:
        result["after"] = page.cursor
    if page.limit is not None:
        result["limit"] = page.limit
    if page.order is not None:
        result["order"] = page.order
    return result


def page_of[T](raw: Object, decode: Callable[[Object], T]) -> Page[T]:
    data = objects(raw["data"])
    more = raw["has_more"]
    if not isinstance(more, bool):
        raise ValueError("invalid has_more")
    return Page(
        data=tuple(decode(v) for v in data),
        has_more=more,
        next_cursor=text(raw["last_id"]) if more else None,
    )


class Context:
    def __init__(
        self,
        transport: Transport,
        account_scope_id: str,
        profile_id: str,
        authorization: Authorization | None,
    ) -> None:
        self.transport = transport
        self.account_scope_id = account_scope_id
        self.profile_id = profile_id
        self._authorization = authorization

    def authorize(self, scope: Scope, kind: str, id_: str | None = None) -> None:
        if scope.is_platform:
            return
        if self._authorization is None or not self._authorization(scope, kind, id_):
            raise ScopeViolation(id_ or kind, "resource requires host authorization")

    def ref(self, scope: Scope, kind: str, id_: str) -> ResourceRef:
        return ResourceRef(
            id=id_,
            kind=kind,
            provider="openai",
            account_scope_id=self.account_scope_id,
            tenant_id=scope.tenant_id,
            account_id=scope.account_id,
        )

    def check(self, scope: Scope, ref: ResourceRef, kind: str) -> None:
        if (
            ref.provider != "openai"
            or ref.account_scope_id != self.account_scope_id
            or ref.kind != kind
        ):
            raise ScopeViolation(ref.id, "foreign provider account or resource kind")
        if not scope.is_platform and (
            ref.tenant_id != scope.tenant_id or ref.account_id != scope.account_id
        ):
            raise ScopeViolation(ref.id, "foreign tenant or account")
        self.authorize(scope, kind, ref.id)

    def record(self, scope: Scope, raw: Object) -> None:
        metadata = object_json(raw.get("metadata") or {})
        if not scope.is_platform and metadata.get("mux_tenant", scope.tenant_id) != scope.tenant_id:
            raise ScopeViolation(text(raw["id"]), "foreign tenant record")

    def unsupported(self, capability: str) -> UnsupportedCapability:
        return UnsupportedCapability((capability,), self.profile_id)

    async def call(
        self,
        method: str,
        path: str,
        *,
        body: Object | None = None,
        params: Mapping[str, str | int] | None = None,
        key: str | None = None,
    ) -> Object:
        # Keep transport literals checked centrally.
        if method not in ("GET", "POST", "DELETE"):
            raise ValueError("unsupported method")
        return await self.transport.request(method, path, body=body, query=params, key=key)


def owned[**P, R](
    function: Callable[P, Awaitable[R]],
) -> Callable[P, CoroutineType[Any, Any, R]]:
    """Malformed provider data never escapes as SDK or validation exceptions."""

    @wraps(function)
    async def wrapped(*args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return await function(*args, **kwargs)
        except (ValueError, KeyError, TypeError):
            raise ProviderError(
                "upstream", retryable=False, native_code="malformed_response"
            ) from None

    return wrapped
