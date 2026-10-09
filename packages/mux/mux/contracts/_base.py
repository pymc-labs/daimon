"""The base every contract type derives from, and its immutable mapping."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from types import MappingProxyType
from typing import Annotated

from pydantic import AfterValidator, BaseModel, ConfigDict, WrapSerializer


class Contract(BaseModel):
    """A frozen, closed DTO.

    Frozen because a contract value is shared between the host, the state
    store and a driver, and none of them may change it under the others.
    Closed (`extra="forbid"`) so a misspelt field fails at the boundary
    instead of travelling as data nobody reads. Mapping fields use
    `FrozenMap`, so freezing reaches one level further than pydantic's
    `frozen` does.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", validate_default=True)


class Tagged(Contract):
    """A member of a discriminated union, tagged by its `type` field.

    The tag always counts as set, so `model_dump(exclude_unset=True)` (the
    way patches serialize) never drops it and the union can still be parsed.
    """

    def model_post_init(self, context: object, /) -> None:
        self.__pydantic_fields_set__.add("type")


def _freeze[K, V](value: Mapping[K, V]) -> Mapping[K, V]:
    return MappingProxyType(dict(value))


def _thaw[K, V](value: Mapping[K, V], handler: Callable[[dict[K, V]], object]) -> object:
    return handler(dict(value))


type FrozenMap[K, V] = Annotated[Mapping[K, V], AfterValidator(_freeze), WrapSerializer(_thaw)]
"""A mapping field that cannot be changed after validation.

Validated into a read-only view, so `revision.requires.clear()` raises
instead of quietly invalidating a digest. JSON values nested inside stay
plain lists and dicts.
"""
