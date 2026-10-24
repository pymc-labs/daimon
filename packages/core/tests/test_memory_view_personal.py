"""The `memory mine` / `memory forget-mine` logic shared by Discord, Slack and Teams."""

from __future__ import annotations

import pytest
from anthropic import AsyncAnthropic
from daimon.core.memory_view import (
    MemoryRequest,
    PersonalMemoryReply,
    is_strict_memory_args,
    parse_memory_args,
    personal_memory_reply,
)
from mux.testing.drivers.anthropic.ma import (
    FakeMemoryStoreState,
    build_fake_anthropic,
    make_fake_memory_store_handler,
)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (None, MemoryRequest("shared_list")),
        ("", MemoryRequest("shared_list")),
        ("/notes/a.md", MemoryRequest("shared_show", "/notes/a.md")),
        ("mine", MemoryRequest("mine_list")),
        ("MINE /style.md", MemoryRequest("mine_show", "/style.md")),
        ("forget-mine", MemoryRequest("forget_ask")),
        ("forget-mine abc123", MemoryRequest("forget_confirm", "abc123")),
    ],
)
def test_parse_memory_args(text: str | None, expected: MemoryRequest) -> None:
    assert parse_memory_args(text) == expected


@pytest.mark.parametrize(
    ("text", "strict"),
    [
        ("/notes/a.md", True),
        ("mine", True),
        ("mine /a.md", True),
        ("mine is the best agent", False),
        ("forget-mine", True),
        ("forget-mine abc123", True),
        ("forget-mine everything please", False),
        ("what do you remember", False),
    ],
)
def test_strict_args_tell_a_command_from_prose(text: str, strict: bool) -> None:
    assert is_strict_memory_args(text) is strict


async def _store(client: AsyncAnthropic, files: dict[str, str]) -> str:
    store = await client.beta.memory_stores.create(name="personal x", description="d")
    for path, content in files.items():
        await client.beta.memory_stores.memories.create(store.id, path=path, content=content)
    return store.id


async def _code(client: AsyncAnthropic, store_id: str) -> str:
    """The code the `forget-mine` preview offers for the store as it is now."""
    asked = await _reply(client, store_id, MemoryRequest("forget_ask"))
    assert asked.hint is not None
    return asked.hint.split()[-1]


async def _reply(
    client: AsyncAnthropic, store_id: str | None, request: MemoryRequest
) -> PersonalMemoryReply:
    return await personal_memory_reply(
        client, agent_name="daimon", store_id=store_id, request=request, command="/memory"
    )


async def test_forget_needs_the_code_it_was_shown_and_touches_no_other_store() -> None:
    state = FakeMemoryStoreState()
    client = build_fake_anthropic(make_fake_memory_store_handler(state))
    mine = await _store(client, {"/style.md": "tabs", "/tone.md": "brief"})
    theirs = await _store(client, {"/style.md": "spaces"})

    asked = await _reply(client, mine, MemoryRequest("forget_ask"))
    assert len(state.memories[mine]) == 2, "asking deletes nothing"
    code = await _code(client, mine)
    assert asked.hint is not None and code in asked.hint

    wrong = await _reply(client, mine, MemoryRequest("forget_confirm", await _code(client, theirs)))
    assert wrong.title.startswith("Nothing was deleted")
    assert len(state.memories[mine]) == 2

    done = await _reply(client, mine, MemoryRequest("forget_confirm", code))
    assert done.title.startswith("Deleted 2 files")
    assert state.memories[mine] == []
    assert len(state.memories[theirs]) == 1, "another store is never touched"


async def test_a_code_goes_stale_when_the_memory_changes() -> None:
    state = FakeMemoryStoreState()
    client = build_fake_anthropic(make_fake_memory_store_handler(state))
    mine = await _store(client, {"/style.md": "tabs"})
    code = await _code(client, mine)
    await client.beta.memory_stores.memories.create(mine, path="/new.md", content="x")

    reply = await _reply(client, mine, MemoryRequest("forget_confirm", code))

    assert reply.title.startswith("Nothing was deleted")
    assert len(state.memories[mine]) == 2


async def test_list_and_show_read_the_given_store() -> None:
    state = FakeMemoryStoreState()
    client = build_fake_anthropic(make_fake_memory_store_handler(state))
    mine = await _store(client, {"/style.md": "tabs"})

    listed = await _reply(client, mine, MemoryRequest("mine_list"))
    shown = await _reply(client, mine, MemoryRequest("mine_show", "/style.md"))
    none = await _reply(client, None, MemoryRequest("mine_list"))

    assert listed.paths == ("/style.md",)
    assert shown.content == "tabs"
    assert none.title == "You have no personal memory with daimon."


async def test_forget_deletes_only_the_previewed_memories_and_keeps_new_ones() -> None:
    """A live conversation writes a memory between the preview and the deletion:
    the code still matches the snapshot it was checked against, so the previewed
    memory goes, and the new one, which nobody confirmed, is kept and reported."""
    import httpx

    state = FakeMemoryStoreState()
    handler = make_fake_memory_store_handler(state)
    armed = False
    store_id = ""

    def concurrent_write(request: httpx.Request) -> httpx.Response:
        nonlocal armed
        response = handler(request)
        if (
            armed
            and request.method == "GET"
            and request.url.path == f"/v1/memory_stores/{store_id}/memories"
        ):
            armed = False
            # Right after the confirmation's own listing: a chat writes a file.
            new = {**state.memories[store_id][0], "id": "mem_new", "path": "/new.md"}
            state.memories[store_id].append({**new, "content_sha256": "f" * 64})
        return response

    client = build_fake_anthropic(concurrent_write)
    store_id = await _store(client, {"/old.md": "old preference"})
    code = await _code(client, store_id)
    armed = True

    reply = await _reply(client, store_id, MemoryRequest("forget_confirm", code))

    assert [m["path"] for m in state.memories[store_id]] == ["/new.md"]
    assert reply.title.startswith("Deleted 1 file ")
    assert "Kept 1 file" in reply.title


async def test_forget_keeps_a_memory_whose_content_changed_after_the_preview() -> None:
    """The deletion carries each memory's listed content hash: one rewritten after
    the preview fails that precondition and is kept."""
    import httpx

    state = FakeMemoryStoreState()
    handler = make_fake_memory_store_handler(state)
    store_id = ""
    rewritten = False

    def rewrite_before_delete(request: httpx.Request) -> httpx.Response:
        nonlocal rewritten
        if request.method == "DELETE" and not rewritten:
            rewritten = True
            state.memories[store_id][0]["content_sha256"] = "e" * 64
        return handler(request)

    client = build_fake_anthropic(rewrite_before_delete)
    store_id = await _store(client, {"/style.md": "tabs"})
    code = await _code(client, store_id)

    reply = await _reply(client, store_id, MemoryRequest("forget_confirm", code))

    assert [m["path"] for m in state.memories[store_id]] == ["/style.md"]
    assert reply.title.startswith("Deleted 0 files") and "Kept 1 file" in reply.title
