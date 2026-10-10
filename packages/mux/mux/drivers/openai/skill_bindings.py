"""Persist neutral skill intent in agent metadata; install in hosted sessions.

Agents have no native skills field. This is an explicit driver compilation,
not a claim that the provider agent resource has that field.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Sequence

from pydantic import JsonValue, TypeAdapter

from mux.contracts.ids import Scope, SkillRef
from mux.drivers.openai._common import Context, text
from mux.drivers.openai.transport import Object, object_json, segment

KEY = "mux_skill_pins"
_PINS = TypeAdapter(tuple[SkillRef, ...])
_VERSION = re.compile(r"[1-9][0-9]*\Z")


def encode(pins: Sequence[SkillRef], context: Context) -> str:
    for pin in pins:
        if (
            pin.source is not None
            or pin.digest is not None
            or (pin.version is not None and _VERSION.fullmatch(pin.version) is None)
        ):
            raise context.unsupported("skill_catalogue_or_version")
        text(pin.id)
    value = json.dumps(
        [pin.model_dump(mode="json", exclude_none=True) for pin in pins], separators=(",", ":")
    )
    return value


def reserved(key: str) -> bool:
    return key == KEY or key.startswith(KEY + "_")


def encode_metadata(pins: Sequence[SkillRef], context: Context) -> Object:
    """Retain the legacy single value; larger lists use integrity-checked chunks."""
    value = encode(pins, context)
    if len(value) <= 512:
        return {KEY: value}
    chunks = [value[i : i + 512] for i in range(0, len(value), 512)]
    if len(chunks) > 8:
        raise context.unsupported("skill_metadata_size")
    digest = hashlib.sha256(value.encode()).hexdigest()
    return {
        KEY: f"chunks:{len(chunks)}:{digest}",
        **{f"{KEY}_{i}": chunk for i, chunk in enumerate(chunks)},
    }


def decode(metadata: JsonValue) -> tuple[SkillRef, ...] | None:
    raw = object_json(metadata or {})
    value = raw.get(KEY)
    if value is None:
        if any(reserved(key) for key in raw):
            raise ValueError("skills chunk marker missing")
        return None
    if not isinstance(value, str):
        raise ValueError("invalid skills metadata")
    chunks = {key for key in raw if key.startswith(KEY + "_")}
    if value.startswith("chunks:"):
        marker = re.fullmatch(r"chunks:([1-8]):([a-f0-9]{64})", value)
        if marker is None:
            raise ValueError("invalid skills chunk marker")
        count, digest = int(marker[1]), marker[2]
        keys = tuple(f"{KEY}_{i}" for i in range(count))
        if chunks != set(keys):
            raise ValueError("missing or extra skills chunks")
        values = tuple(text(raw[key]) for key in keys)
        if any(len(chunk) > 512 for chunk in values):
            raise ValueError("oversized skills chunk")
        value = "".join(values)
        if hashlib.sha256(value.encode()).hexdigest() != digest:
            raise ValueError("changed skills chunks")
    elif chunks:
        raise ValueError("unexpected skills chunks")
    pins = _PINS.validate_json(value)
    for pin in pins:
        text(pin.id)
        if (
            pin.source is not None
            or pin.digest is not None
            or (pin.version is not None and _VERSION.fullmatch(pin.version) is None)
        ):
            raise ValueError("invalid stored skill pin")
    return pins


def compile_pins(pins: Sequence[SkillRef], scope: Scope, context: Context) -> list[JsonValue]:
    result: list[JsonValue] = []
    for pin in pins:
        context.authorize(scope, "skill", pin.id)
        raw: Object = {"type": "skill_reference", "skill_id": pin.id}
        if pin.version is not None:
            raw["version"] = pin.version
        result.append(raw)
    return result


async def resolve_pins(
    pins: Sequence[SkillRef], scope: Scope, context: Context
) -> tuple[SkillRef, ...]:
    # All grants are checked before reading the first native skill version.
    compile_pins(pins, scope, context)
    result: list[SkillRef] = []
    for pin in pins:
        path = "/skills/" + segment(pin.id)
        if pin.version is not None:
            path += "/versions/" + segment(pin.version)
        raw = await context.call("GET", path)
        if pin.version is None:
            if raw.get("id") != pin.id:
                raise ValueError("wrong skill identity")
        elif raw.get("skill_id") != pin.id or raw.get("version") != pin.version:
            raise ValueError("wrong skill version identity")

        version = text(raw["default_version"] if pin.version is None else raw["version"])
        if _VERSION.fullmatch(version) is None:
            raise ValueError("invalid resolved skill version")
        result.append(pin.model_copy(update={"version": version}))
    return tuple(result)


async def verify_pins(pins: Sequence[SkillRef], scope: Scope, context: Context) -> None:
    await resolve_pins(pins, scope, context)
