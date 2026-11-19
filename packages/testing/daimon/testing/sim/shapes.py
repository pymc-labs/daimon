"""Strict request shapes: a body must match the provider SDK's own param type exactly.

The SDKs' TypedDicts are the closest offline copy of each API's request schema. A key
the type does not declare is refused the way the API refuses it (#1394: a fake that
echoed an unknown `spend_control` hid an HTTP 400 on every OpenAI session create).
"""

from __future__ import annotations

import collections.abc
import enum
import types
import typing
from typing import Any, Literal, NotRequired, Required, Union, cast, get_args, get_origin

import typing_extensions as te
from pydantic import BaseModel

# Keys an SDK TypedDict carries for the client, never sent in the body.
_CLIENT_ONLY = frozenset({"betas", "extra_headers", "extra_query", "extra_body", "timeout"})
# A decoded JSON body and an SDK type hint: both dynamic by nature.
_Json = Any
_Hint = Any
_SEQUENCES = ("Sequence", "SequenceNotStr", "Iterable", "list", "tuple", "List")


def violations(value: _Json, hint: _Hint, path: str = "$") -> list[str]:
    """Every place `value` departs from `hint`, as `path: reason`; empty when it fits."""
    origin, args = get_origin(hint), get_args(hint)
    if hint in (Any, object) or isinstance(hint, typing.TypeVar):
        return []
    if origin in (Required, NotRequired, typing.Annotated, te.Annotated):
        return violations(value, args[0], path)
    if isinstance(hint, typing.TypeAliasType | te.TypeAliasType):
        return violations(value, hint.__value__, path)
    if origin in (Union, types.UnionType):
        found = [violations(value, arm, path) for arm in args]
        return [] if any(not v for v in found) else min(found, key=len)
    if hint is None or hint is type(None):
        return [] if value is None else [f"{path}: expected null"]
    if origin is Literal:
        return [] if value in args else [f"{path}: {value!r} is not one of {list(args)}"]
    if te.is_typeddict(hint):
        return _typed_dict(value, hint, path)
    if isinstance(hint, type) and issubclass(hint, BaseModel):
        return _model(value, hint, path)
    if isinstance(hint, type) and issubclass(hint, enum.Enum):
        return [] if value in {m.value for m in hint} else [f"{path}: {value!r} not in enum"]
    name = getattr(origin or hint, "__name__", "")
    if origin in (dict, collections.abc.Mapping) or hint is dict:
        if not isinstance(value, dict):
            return [f"{path}: expected object"]
        item = args[1] if len(args) == 2 else Any
        obj = cast("dict[str, _Json]", value)
        return [v for key, sub in obj.items() for v in violations(sub, item, f"{path}.{key}")]
    if name in _SEQUENCES:
        if not isinstance(value, list):
            return [f"{path}: expected array"]
        item = args[0] if args else Any
        rows = cast("list[_Json]", value)
        return [v for i, sub in enumerate(rows) for v in violations(sub, item, f"{path}[{i}]")]
    return _scalar(value, hint, path)


def _typed_dict(value: _Json, hint: _Hint, path: str) -> list[str]:
    if not isinstance(value, dict):
        return [f"{path}: expected object"]
    value = cast("dict[str, _Json]", value)
    fields = te.get_type_hints(hint, include_extras=True)
    unknown = sorted(set(value) - set(fields))
    found = [f"{path}.{key}: Unknown parameter" for key in unknown]
    required = set(hint.__required_keys__) - _CLIENT_ONLY
    found += [f"{path}.{key}: Field required" for key in sorted(required - set(value))]
    for key, sub in value.items():
        if key in fields:
            found += violations(sub, fields[key], f"{path}.{key}")
    return found


def _model(value: _Json, model: type[BaseModel], path: str) -> list[str]:
    if not isinstance(value, dict):
        return [f"{path}: expected object"]
    value = cast("dict[str, _Json]", value)
    fields = {(info.alias or name): info for name, info in model.model_fields.items()}
    found = [f"{path}.{key}: Unknown parameter" for key in sorted(set(value) - set(fields))]
    for key, info in fields.items():
        if key in value:
            found += violations(value[key], info.annotation, f"{path}.{key}")
        elif info.is_required():
            found.append(f"{path}.{key}: Field required")
    return found


def _scalar(value: _Json, hint: _Hint, path: str) -> list[str]:
    if hint is bool:
        ok = isinstance(value, bool)
    elif hint is int:
        ok = isinstance(value, int) and not isinstance(value, bool)
    elif hint is float:
        ok = isinstance(value, int | float) and not isinstance(value, bool)
    elif hint in (str, bytes):
        ok = isinstance(value, str)
    else:
        # Dates, file types and other SDK helpers arrive as JSON strings or numbers.
        ok = isinstance(value, str | int | float)
    return [] if ok else [f"{path}: expected {getattr(hint, '__name__', hint)}"]
