"""The base every contract type derives from, and its immutable mapping."""

from __future__ import annotations

import copy
from collections.abc import Callable, Iterator, Mapping
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


class _ReadOnlyMap[K, V](Mapping[K, V]):
    """An immutable mapping: no mutators, and it pickles and copies.

    `MappingProxyType` cannot be pickled or deep-copied, which a state store
    or cache needs; this keeps a private dict and exposes only reads.
    """

    __slots__ = ("_data",)

    def __init__(self, data: Mapping[K, V]) -> None:
        self._data = dict(data)

    def __getitem__(self, key: K) -> V:
        return self._data[key]

    def __iter__(self) -> Iterator[K]:
        return iter(self._data)

    def __len__(self) -> int:
        return len(self._data)

    def __eq__(self, other: object) -> bool:
        return self._data == other if isinstance(other, Mapping) else NotImplemented

    __hash__ = None  # pyright: ignore[reportAssignmentType]

    def __repr__(self) -> str:
        return repr(self._data)

    def __reduce__(self) -> tuple[type[_ReadOnlyMap[K, V]], tuple[dict[K, V]]]:
        return (type(self), (self._data,))

    def __copy__(self) -> _ReadOnlyMap[K, V]:
        return self

    def __deepcopy__(self, memo: dict[int, object]) -> _ReadOnlyMap[K, V]:
        return type(self)(copy.deepcopy(self._data, memo))


def _freeze[K, V](value: Mapping[K, V]) -> Mapping[K, V]:
    return value if isinstance(value, _ReadOnlyMap) else _ReadOnlyMap(value)


def _thaw[K, V](value: Mapping[K, V], handler: Callable[[dict[K, V]], object]) -> object:
    return handler(dict(value))


type FrozenMap[K, V] = Annotated[Mapping[K, V], AfterValidator(_freeze), WrapSerializer(_thaw)]
"""A mapping field that cannot be changed after validation.

Validated into a read-only mapping, so `revision.requires.clear()` raises
instead of quietly invalidating a digest. JSON values nested inside stay
plain lists and dicts.
"""
