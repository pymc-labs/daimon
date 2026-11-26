"""Persist neutral skill intent in agent metadata; install in hosted sessions.

Agents have no native skills field. This is an explicit driver compilation,
not a claim that the provider agent resource has that field.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
from collections.abc import Sequence

from pydantic import JsonValue, TypeAdapter

from mux.contracts.ids import Scope, SkillRef
from mux.drivers.openai._common import Context, text
from mux.drivers.openai.skills import retry_not_found, verify_content
from mux.drivers.openai.transport import Object, object_json, segment

KEY = "mux_skill_pins"
_PINS = TypeAdapter(tuple[SkillRef, ...])
_VERSION = re.compile(r"[1-9][0-9]*\Z")
#: OpenAI caps each metadata value at 512 characters.
_VALUE = 512
#: The legacy chunk form (JSON pins) holds 8 chunks; the compact form up to 12, within
#: the 16 metadata keys a native object has, and the agent path still refuses past 15.
_CHUNKS_V2 = 12
_DIGEST_B64 = re.compile(r"[A-Za-z0-9_-]{43}\Z")


def encode(pins: Sequence[SkillRef], context: Context) -> str:
    for pin in pins:
        if (
            pin.source is not None
            or (
                pin.digest is not None
                and (pin.version is None or re.fullmatch(r"[a-f0-9]{64}", pin.digest) is None)
            )
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


def _compact(pins: Sequence[SkillRef], context: Context) -> str:
    """The ids' shared prefix and `;`, then `id|version|digest` per pin without it, comma-joined.

    An unset field is empty; a digest is base64url.
    """
    if any(sep in pin.id for pin in pins for sep in "|,;"):
        raise context.unsupported("skill_catalogue_or_version")
    prefix = os.path.commonprefix([pin.id for pin in pins])
    fields: list[str] = []
    for pin in pins:
        digest = (
            base64.urlsafe_b64encode(bytes.fromhex(pin.digest)).decode().rstrip("=")
            if pin.digest is not None
            else ""
        )
        fields.append(f"{pin.id[len(prefix) :]}|{pin.version or ''}|{digest}")
    return prefix + ";" + ",".join(fields)


def encode_metadata(pins: Sequence[SkillRef], context: Context) -> Object:
    """A short list as one legacy JSON value; a longer one as integrity-checked chunks.

    The chunks hold the compact form (`chunks2:`), so a list of digest-pinned skills fits;
    `decode` still reads the JSON chunks (`chunks:`) that existing agents carry.
    """
    value = encode(pins, context)
    if len(value) <= _VALUE:
        return {KEY: value}
    payload = _compact(pins, context)
    chunks = [payload[i : i + _VALUE] for i in range(0, len(payload), _VALUE)]
    if len(chunks) > _CHUNKS_V2:
        raise context.unsupported("skill_metadata_size")
    digest = hashlib.sha256(payload.encode()).hexdigest()
    return {
        KEY: f"chunks2:{len(chunks)}:{digest}",
        **{f"{KEY}_{i}": chunk for i, chunk in enumerate(chunks)},
    }


def _chunked(raw: Object, chunks: set[str], *, count: int, digest: str) -> str:
    keys = tuple(f"{KEY}_{i}" for i in range(count))
    if chunks != set(keys):
        raise ValueError("missing or extra skills chunks")
    values = tuple(text(raw[key]) for key in keys)
    if any(len(chunk) > _VALUE for chunk in values):
        raise ValueError("oversized skills chunk")
    joined = "".join(values)
    if hashlib.sha256(joined.encode()).hexdigest() != digest:
        raise ValueError("changed skills chunks")
    return joined


def _expand(payload: str) -> str:
    """The JSON the compact form stands for."""
    prefix, sep, body = payload.partition(";")
    if not sep:
        raise ValueError("invalid compact skill pins")
    pins: list[dict[str, str]] = []
    for item in body.split(","):
        fields = item.split("|")
        if len(fields) != 3:
            raise ValueError("invalid compact skill pin")
        pin = {"id": prefix + fields[0]}
        if fields[1]:
            pin["version"] = fields[1]
        if fields[2]:
            if _DIGEST_B64.fullmatch(fields[2]) is None:
                raise ValueError("invalid compact skill digest")
            raw = base64.urlsafe_b64decode(fields[2] + "=")
            if base64.urlsafe_b64encode(raw).decode().rstrip("=") != fields[2] or len(raw) != 32:
                raise ValueError("invalid compact skill digest")
            pin["digest"] = raw.hex()
        pins.append(pin)
    return json.dumps(pins, separators=(",", ":"))


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
        value = _chunked(raw, chunks, count=int(marker[1]), digest=marker[2])
    elif value.startswith("chunks2:"):
        marker = re.fullmatch(r"chunks2:([1-9][0-9]?):([a-f0-9]{64})", value)
        if marker is None or int(marker[1]) > _CHUNKS_V2:
            raise ValueError("invalid skills chunk marker")
        value = _expand(_chunked(raw, chunks, count=int(marker[1]), digest=marker[2]))
    elif chunks:
        raise ValueError("unexpected skills chunks")
    pins = _PINS.validate_json(value)
    for pin in pins:
        text(pin.id)
        if (
            pin.source is not None
            or (
                pin.digest is not None
                and (pin.version is None or re.fullmatch(r"[a-f0-9]{64}", pin.digest) is None)
            )
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
        raw = await retry_not_found(lambda path=path: context.call("GET", path))
        if pin.version is None:
            if raw.get("id") != pin.id:
                raise ValueError("wrong skill identity")
        elif raw.get("skill_id") != pin.id or raw.get("version") != pin.version:
            raise ValueError("wrong skill version identity")

        version = text(raw["default_version"] if pin.version is None else raw["version"])
        if _VERSION.fullmatch(version) is None:
            raise ValueError("invalid resolved skill version")
        if pin.digest is not None:
            await verify_content(context, pin.id, version, pin.digest)
        result.append(pin.model_copy(update={"version": version}))
    return tuple(result)


async def verify_pins(pins: Sequence[SkillRef], scope: Scope, context: Context) -> None:
    await resolve_pins(pins, scope, context)
