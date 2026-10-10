"""Eleven realistic immutable pins survive native metadata limits and corruption."""

from __future__ import annotations

import pytest
from mux.contracts.ids import SkillRef
from mux.drivers.openai._common import Context
from mux.drivers.openai.skill_bindings import KEY, decode, encode_metadata
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
    assert str(encoded[KEY]).startswith("chunks:")
    assert len(encoded) <= 9 and all(
        isinstance(value, str) and len(value) <= 512 for value in encoded.values()
    )
    assert decode(encoded) == pins()


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
        encode_metadata(tuple(SkillRef(id="x" * 500, version="1") for _ in range(12)), context)
