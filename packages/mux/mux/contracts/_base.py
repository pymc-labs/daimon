"""The base every contract type derives from."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class Contract(BaseModel):
    """A frozen, closed DTO.

    Frozen because a contract value is shared between the host, the state
    store and a driver, and none of them may change it under the others.
    Closed (`extra="forbid"`) so a misspelt field fails at the boundary
    instead of travelling as data nobody reads.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")


class Tagged(Contract):
    """A member of a discriminated union, tagged by its `type` field.

    The tag always counts as set, so `model_dump(exclude_unset=True)` (the
    way patches serialize) never drops it and the union can still be parsed.
    """

    def model_post_init(self, context: object, /) -> None:
        self.__pydantic_fields_set__.add("type")
