"""Closed response snapshots for the temporary, operation-specific M0 codecs."""

from typing import cast

from pydantic import BaseModel, JsonValue

from mux.drivers.anthropic.schemas import NativeConfig


class NativeSnapshot(NativeConfig):
    """Preserve SDK-accepted fields without validating unused response properties."""

    native: dict[str, JsonValue]


def native_snapshot(item: BaseModel) -> NativeSnapshot:
    return NativeSnapshot(
        native=cast(
            dict[str, JsonValue], item.model_dump(mode="json", exclude_unset=True, warnings=False)
        )
    )
