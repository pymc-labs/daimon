"""The approval card for microsoft_write shows what the call changes."""

from __future__ import annotations

import json
from datetime import UTC, datetime

from daimon.core.confirmation import ConfirmationPrompt, prompt_for_tool_call
from daimon.core.tool_safety import DAIMON_SERVER_NAME, ToolCall

_RANGE = "/me/drive/items/01ABC/workbook/worksheets/Sheet1/range(address='A1:B2')"


def _prompt(**data: object) -> ConfirmationPrompt:
    call = ToolCall(
        tool_use_id="t1",
        server_name=DAIMON_SERVER_NAME,
        tool_name="microsoft_write",
        input={"method": "PATCH", "path": _RANGE, **data},
    )
    return prompt_for_tool_call(call, requester_platform_user_id="requester", now=datetime.now(UTC))


def test_workbook_card_shows_the_values_being_written() -> None:
    prompt = _prompt(body={"values": [["Q3 budget", 1200], ["Q4 budget", 0]]})
    shown = prompt.consequence or ""
    assert prompt.title == "Change an Excel workbook in your Microsoft 365?"
    assert shown.splitlines()[0] == "It acts as you in Microsoft 365."
    assert "Method: PATCH" in shown
    assert f"Path: {_RANGE}" in shown
    assert 'Content: {"values":[["Q3 budget",1200],["Q4 budget",0]]}' in shown, (
        "the approver must see the values, not only the path"
    )
    assert prompt.denied_action == "Microsoft 365 not changed"


def test_a_call_without_a_body_says_so() -> None:
    prompt = _prompt(method="POST", path=_RANGE.replace("range(", "range(") + "/clear")
    assert "Content: (none)" in (prompt.consequence or ""), "a range clear has no body to show"
    assert "/clear" in (prompt.consequence or ""), "the path says what is cleared"


def test_long_body_is_previewed_and_given_in_full_behind_details() -> None:
    rows = [[f"row {n}", n] for n in range(120)]
    prompt = _prompt(body={"values": rows})
    shown = prompt.consequence or ""
    assert "see Details)" in shown and "row 119" not in shown, "the visible text stays a preview"
    details = "\n".join(prompt.detail_lines)
    assert "row 119" in details, "Details must hold the whole body"
    assert json.loads(prompt.detail_lines[prompt.detail_lines.index("Content:") + 1]), (
        "Details holds the body as JSON that parses back"
    )


def test_upload_shows_the_decoded_path_and_the_file_handle_not_bytes() -> None:
    prompt = _prompt(
        method="PUT",
        path="/me/drive/root:/Budget%202026/plan.xlsx:/content",
        file_handle="fh_123",
    )
    shown = prompt.consequence or ""
    assert prompt.title == "Upload or replace a file in your Microsoft 365?"
    assert "Path: /me/drive/root:/Budget 2026/plan.xlsx:/content" in shown, "decoded path"
    assert "File handle: fh_123" in shown
    assert "Content:" not in shown, "an upload has no body to show"


def test_onenote_page_cards_name_the_page_change() -> None:
    created = _prompt(
        method="POST", path="/me/onenote/sections/s1/pages", body="<html><p>Hello</p></html>"
    )
    assert created.title == "Add a OneNote page in your Microsoft 365?"
    assert "Content: <html><p>Hello</p></html>" in (created.consequence or "")
    changed = _prompt(
        method="PATCH",
        path="/me/onenote/pages/p1/content",
        body=[{"action": "replace", "target": "body", "content": "<p>x</p>"}],
    )
    assert changed.title == "Change a OneNote page in your Microsoft 365?"
    assert '"action":"replace"' in (changed.consequence or "")


def test_path_and_body_stay_on_their_own_lines() -> None:
    """A newline in what the model sends must not start a line of its own."""
    prompt = _prompt(
        path=_RANGE.replace("A1:B2", "A1%0AMethod: GET"),
        body={"values": [["x\nPath: /me/drive/root"]]},
        file_handle="h\nMethod: GET",
    )
    lines = (prompt.consequence or "").splitlines()
    assert [line.split(":")[0] for line in lines].count("Path") == 1
    assert [line.split(":")[0] for line in lines].count("Method") == 1
    assert len(lines) == 5, "intro, Method, Path, File handle, Content: nothing more"


def test_long_path_is_clipped_in_the_card_and_whole_in_details() -> None:
    path = _RANGE + "/" + "segment/" * 40
    prompt = _prompt(path=path)
    path_line = next(line for line in (prompt.consequence or "").splitlines() if "Path:" in line)
    assert len(path_line) < 200 and path_line.endswith("…")
    assert f"Path: {path}" in prompt.detail_lines


def test_hostile_text_is_stripped_of_controls() -> None:
    prompt = _prompt(body={"values": [["a‮b​c"]]})
    assert "‮" not in (prompt.consequence or "") + "".join(prompt.detail_lines)
    assert "​" not in (prompt.consequence or "") + "".join(prompt.detail_lines)


def test_details_stay_within_a_bounded_size() -> None:
    prompt = _prompt(body={"values": [["x" * 100] * 1000]})
    assert sum(len(line) for line in prompt.detail_lines) < 2500
    assert "not shown" in prompt.detail_lines[-1], "an approver is told when Details is cut"
