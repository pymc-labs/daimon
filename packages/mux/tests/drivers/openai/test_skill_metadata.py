"""Eleven realistic immutable pins survive native metadata limits and corruption."""

from __future__ import annotations

import hashlib

import pytest
from mux.contracts.ids import SkillRef
from mux.drivers.openai._common import Context
from mux.drivers.openai.skill_bindings import KEY, decode, encode, encode_metadata
from mux.drivers.openai.transport import Object
from mux.errors import UnsupportedCapability

from .conftest import FakeTransport


def pins() -> tuple[SkillRef, ...]:
    return tuple(SkillRef(id="skill_" + "x" * 42 + str(n), version="1") for n in range(11))


def metadata() -> Object:
    return encode_metadata(
        pins(), Context(FakeTransport(), "offline", "openai.persistent_workspace", None)
    )


def test_eleven_long_ids_round_trip_in_bounded_integrity_checked_chunks() -> None:
    encoded = metadata()
    assert str(encoded[KEY]).startswith("chunks2:")
    assert len(encoded) <= 9 and all(
        isinstance(value, str) and len(value) <= 512 for value in encoded.values()
    )
    assert decode(encoded) == pins()


def _legacy_chunks(pins: tuple[SkillRef, ...]) -> Object:
    """The JSON chunk form agents written before the compact form carry."""
    value = encode(pins, Context(FakeTransport(), "offline", "openai.persistent_workspace", None))
    chunks = [value[i : i + 512] for i in range(0, len(value), 512)]
    return {
        KEY: f"chunks:{len(chunks)}:{hashlib.sha256(value.encode()).hexdigest()}",
        **{f"{KEY}_{i}": chunk for i, chunk in enumerate(chunks)},
    }


def test_json_chunks_written_by_earlier_versions_still_decode() -> None:
    assert decode(_legacy_chunks(pins())) == pins()


def test_digest_pins_round_trip_compactly_and_a_bad_digest_is_refused() -> None:
    context = Context(FakeTransport(), "offline", "openai.persistent_workspace", None)
    digested = tuple(
        SkillRef(id="skill_" + "x" * 42 + str(n), version="3", digest=f"{n:02x}" * 32)
        for n in range(12)
    )
    encoded = encode_metadata(digested, context)
    assert str(encoded[KEY]).startswith("chunks2:")
    assert decode(encoded) == digested, "digests survive the base64 form"
    payload = "".join(str(encoded[f"{KEY}_{i}"]) for i in range(len(encoded) - 1))
    bad = payload.replace(payload.split("|")[2][:3], "!!!", 1)
    encoded[f"{KEY}_0"] = bad[:512]
    encoded[KEY] = (
        "chunks2:"
        + str(encoded[KEY]).split(":", 1)[1].split(":")[0]
        + ":"
        + hashlib.sha256(bad.encode()).hexdigest()
    )
    with pytest.raises(ValueError):
        decode(encoded)


def test_ids_the_compact_form_cannot_carry_are_refused() -> None:
    context = Context(FakeTransport(), "offline", "openai.persistent_workspace", None)
    refused = tuple(SkillRef(id="skill_" + "x" * 60 + "," + str(n), version="1") for n in range(9))
    with pytest.raises(UnsupportedCapability):
        encode_metadata(refused, context)


def test_legacy_single_metadata_value_is_unchanged_and_still_decodes() -> None:
    encoded = encode_metadata(
        (SkillRef(id="short", version="1"),),
        Context(FakeTransport(), "offline", "openai.persistent_workspace", None),
    )
    assert encoded == {KEY: '[{"id":"short","version":"1"}]'}
    assert decode(encoded) == (SkillRef(id="short", version="1"),)


@pytest.mark.parametrize("fault", ["missing", "extra", "corrupt", "marker", "oversized", "orphan"])
def test_malformed_chunk_sets_refuse_without_partial_skill_intent(fault: str) -> None:
    encoded = metadata()
    chunk = KEY + "_0"
    if fault == "missing":
        encoded.pop(chunk)
    elif fault == "extra":
        encoded[KEY + "_9"] = "extra"
    elif fault == "corrupt":
        encoded[chunk] = "changed"
    elif fault == "marker":
        encoded[KEY] = "chunks:999:invalid"
    elif fault == "oversized":
        encoded[chunk] = "x" * 513
    else:
        encoded.pop(KEY)
    with pytest.raises(ValueError):
        decode(encoded)


def test_unbounded_pin_list_still_refuses_before_native_io() -> None:
    context = Context(FakeTransport(), "offline", "openai.persistent_workspace", None)
    with pytest.raises(UnsupportedCapability):
        encode_metadata(tuple(SkillRef(id="x" * 500, version="1") for _ in range(13)), context)


def test_eleven_native_versions_retain_independent_content_pins() -> None:
    original = tuple(pin.model_copy(update={"digest": "a" * 64}) for pin in pins())
    context = Context(FakeTransport(), "offline", "openai.persistent_workspace", None)
    assert decode(encode_metadata(original, context)) == original


@pytest.mark.parametrize("version,digest", [(None, "a" * 64), ("1", "not-a-digest")])
def test_content_pins_require_concrete_version_and_sha256(version, digest) -> None:
    context = Context(FakeTransport(), "offline", "openai.persistent_workspace", None)
    with pytest.raises(UnsupportedCapability):
        encode_metadata((SkillRef(id="native", version=version, digest=digest),), context)
