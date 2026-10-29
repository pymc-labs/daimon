"""The operator registry of pollable tools (`DAIMON_WATCHES__POLLABLE_TOOLS`)."""

import json

import pytest
from daimon.core.config import PollableTool, WatchesSettings, load_watches_settings
from daimon.core.continuity.poll_tools import ToolUnlisted, require_listed
from pydantic import ValidationError

NATIVE = {"server": "daimon-mcp", "credential": "none", "returns_chat": True}


def test_default_registry_lists_both_native_readers_at_the_old_floor(monkeypatch):
    monkeypatch.delenv("DAIMON_WATCHES__POLLABLE_TOOLS", raising=False)
    listed = load_watches_settings().pollable_tools
    assert {(entry.server, entry.tool) for entry in listed} == {
        ("daimon-mcp", "read_thread"),
        ("daimon-mcp", "read_channel"),
    }
    assert all(entry.max_interval_floor_s == 60 for entry in listed)
    assert require_listed("daimon-mcp", "read_thread").credential == "none"


def test_registry_is_read_fresh_from_the_environment(monkeypatch):
    monkeypatch.setenv(
        "DAIMON_WATCHES__POLLABLE_TOOLS", json.dumps([{**NATIVE, "tool": "read_channel"}])
    )
    require_listed("daimon-mcp", "read_channel")
    with pytest.raises(ToolUnlisted):
        require_listed("daimon-mcp", "read_thread")
    monkeypatch.setenv("DAIMON_WATCHES__POLLABLE_TOOLS", "[]")
    with pytest.raises(ToolUnlisted):
        require_listed("daimon-mcp", "read_channel")


def test_mcp_entries_match_by_canonical_url_never_by_name(monkeypatch):
    entry = {
        "server": "https://Bridge.Example/mcp/",
        "tool": "notion_query_database",
        "credential": "owner",
        "returns_chat": False,
    }
    monkeypatch.setenv("DAIMON_WATCHES__POLLABLE_TOOLS", json.dumps([entry]))
    assert require_listed("https://bridge.example/mcp", "notion_query_database")
    for server in ("bridge", "daimon-mcp", "https://other.example/mcp"):
        with pytest.raises(ToolUnlisted):
            require_listed(server, "notion_query_database")


@pytest.mark.parametrize(
    "entry",
    [
        {**NATIVE, "tool": "send_message"},
        {**NATIVE, "tool": "read_thread", "credential": "owner"},
        {
            "server": "http://bridge.example/mcp",
            "tool": "x",
            "credential": "owner",
            "returns_chat": False,
        },
        {
            "server": "https://u:p@bridge.example/mcp",
            "tool": "x",
            "credential": "owner",
            "returns_chat": False,
        },
        {"server": "bridge", "tool": "x", "credential": "owner", "returns_chat": False},
        {
            "server": "https://bridge.example/mcp",
            "tool": "discord_read_messages",
            "credential": "owner",
            "returns_chat": True,
        },
        {"server": "https://bridge.example/mcp", "tool": "x", "credential": "owner"},
        {
            "server": "https://bridge.example/mcp",
            "tool": "a b",
            "credential": "owner",
            "returns_chat": False,
        },
        {**NATIVE, "tool": "read_thread", "max_interval_floor_s": 59},
    ],
)
def test_unsafe_registry_entries_are_refused_at_settings_load(entry):
    with pytest.raises(ValidationError):
        PollableTool.model_validate(entry)


def test_duplicates_and_bad_json_are_refused():
    with pytest.raises(ValidationError, match="more than once"):
        WatchesSettings(pollable_tools=json.dumps([{**NATIVE, "tool": "read_thread"}] * 2))  # pyright: ignore[reportArgumentType]
    with pytest.raises(ValidationError, match="not valid JSON"):
        WatchesSettings(pollable_tools="[{")  # pyright: ignore[reportArgumentType]
    # Blank means the default; only an explicit [] allows nothing.
    default = WatchesSettings().pollable_tools
    assert WatchesSettings(pollable_tools="").pollable_tools == default  # pyright: ignore[reportArgumentType]
    assert WatchesSettings(pollable_tools="[]").pollable_tools == []  # pyright: ignore[reportArgumentType]


@pytest.mark.parametrize(
    "tool", ["connected_archives_search", "discord_read_messages", "fetch_attachment"]
)
def test_known_chat_tools_are_refused_on_mcp_servers_even_marked_not_chat(tool):
    # R5: an operator mistake (returns_chat false on an archive reader) is refused at load.
    with pytest.raises(ValidationError, match="returns chat"):
        PollableTool.model_validate(
            {
                "server": "https://bridge.example/mcp",
                "tool": tool,
                "credential": "agent",
                "returns_chat": False,
            }
        )


def test_internationalised_hosts_are_one_entry_and_match_either_spelling(monkeypatch):
    entry = {"tool": "x", "credential": "agent", "returns_chat": False}
    unicode_host = {**entry, "server": "https://Bücher.example/mcp"}
    punycode_host = {**entry, "server": "https://xn--bcher-kva.example/mcp/"}
    assert PollableTool.model_validate(unicode_host).server == "https://xn--bcher-kva.example/mcp"
    with pytest.raises(ValidationError, match="more than once"):
        WatchesSettings(pollable_tools=json.dumps([unicode_host, punycode_host]))  # pyright: ignore[reportArgumentType]
    monkeypatch.setenv("DAIMON_WATCHES__POLLABLE_TOOLS", json.dumps([unicode_host]))
    assert require_listed("https://xn--bcher-kva.example/mcp", "x")
    assert require_listed("https://bücher.example/mcp/", "x")
