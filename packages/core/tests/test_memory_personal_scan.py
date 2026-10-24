"""The read-only scan of shared memory for entries that look personal."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from daimon.core.memory_personal_scan import (
    line_looks_personal,
    path_looks_personal,
    redact_line,
    redact_path,
    scan_shared_memory,
)
from mux.testing.drivers.anthropic.ma import (
    FakeMemoryStoreState,
    build_fake_anthropic,
    make_fake_memory_store_handler,
)


@pytest.mark.parametrize(
    ("line", "personal"),
    [
        ("<@123456> prefers bullet points over prose", True),
        ("@alice likes short answers", True),
        ("Alice prefers tabs", True),
        ("I want replies in Spanish", True),
        ("my favourite format is tables", True),
        ("The deploy runs on Fridays", False),
        ("Use ruff for lint; the repo prefers 100-char lines", False),
    ],
)
def test_line_heuristic(line: str, personal: bool) -> None:
    assert line_looks_personal(line) is personal


def test_per_person_paths() -> None:
    assert path_looks_personal("/users/alice.md")
    assert path_looks_personal("/people/bob/prefs.md")
    assert not path_looks_personal("/conventions/lint.md")


def test_redaction_leaves_no_raw_content() -> None:
    line = "<@123456> Alice Smith prefers emails at alice@example.com about her divorce"
    redacted = redact_line(line)
    for raw in ("123456", "Alice", "Smith", "alice@example.com", "divorce", "emails"):
        assert raw not in redacted
    assert "prefers" in redacted and "<mention>" in redacted
    assert "[" not in redacted, "no Rich markup brackets"
    assert redact_path("/users/alice-smith.md") == "/users/<redacted>.md"
    assert redact_path("/users/profile.alice") == "/users/profile"
    assert redact_path("/users/name.@bob") == "/users/<redacted>"


async def test_scan_counts_and_redacts_and_only_reads() -> None:
    state = FakeMemoryStoreState()
    methods: list[tuple[str, str]] = []
    handler = make_fake_memory_store_handler(state)

    def recording(request: httpx.Request) -> httpx.Response:
        methods.append((request.method, request.url.path))
        return handler(request)

    client = build_fake_anthropic(recording)
    store = await client.beta.memory_stores.create(name="daimon x", description="d")
    await client.beta.memory_stores.memories.create(
        store.id, path="/notes/team.md", content="Deploys run on Fridays."
    )
    await client.beta.memory_stores.memories.create(
        store.id, path="/notes/style.md", content="Header\n<@42> prefers terse answers"
    )
    await client.beta.memory_stores.memories.create(
        store.id, path="/users/bob.md", content="Timezone is CET."
    )
    before: dict[str, list[dict[str, Any]]] = {
        sid: [dict(m) for m in mems] for sid, mems in state.memories.items()
    }
    methods.clear()

    scan = await scan_shared_memory(client, store.id)

    assert (scan.memories, scan.flagged) == (3, 2)
    assert all(method == "GET" for method, _ in methods), f"read-only, got {methods!r}"
    assert state.memories == before, "nothing moved, edited or deleted"
    joined = " ".join(scan.examples)
    for raw in ("42", "terse", "bob", "CET"):
        assert raw not in joined
