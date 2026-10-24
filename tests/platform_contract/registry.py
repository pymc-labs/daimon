"""Registry of named payload builders.

An entry is a zero-argument callable returning a `Built`: the platform, the kind of
payload, and the payload itself. Builders run lazily, so importing the catalogue is
cheap and one broken builder fails only its own test.

Kinds:
  slack   `view`     a modal or Home-tab view (`views.open` / `views.publish`)
          `blocks`   the `blocks` list of a message
          `message`  a message body with `attachments` (never a top-level `text`;
                     `blocks.validate` rejects it as an additional property)
  discord `message`  a REST body for `POST /channels/{id}/messages`
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Literal, NamedTuple

Platform = Literal["slack", "discord"]
Kind = Literal["view", "blocks", "message"]


class Built(NamedTuple):
    platform: Platform
    kind: Kind
    payload: Any


Builder = Callable[[], Built]

CATALOGUE: dict[str, Builder] = {}


def entry(
    name: str, platform: Platform, kind: Kind
) -> Callable[[Callable[[], Any]], Callable[[], Any]]:
    """Register `fn` (which returns just the payload) as `<platform>/<name>`."""
    key = f"{platform}/{name}"

    def register(fn: Callable[[], Any]) -> Callable[[], Any]:
        if key in CATALOGUE:
            raise ValueError(f"duplicate catalogue entry: {key}")
        CATALOGUE[key] = lambda: Built(platform, kind, fn())
        return fn

    return register
