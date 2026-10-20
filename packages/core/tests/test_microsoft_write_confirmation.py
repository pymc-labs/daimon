"""The approval card for microsoft_write shows what the call changes."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from daimon.core.confirmation import (
    WRITE_BODY_BYTES,
    WRITE_BODY_LIMIT,
    ConfirmationPrompt,
    prompt_for_tool_call,
    write_body_size,
)
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
    assert len(path_line) < 200 and "…" in path_line and path_line.endswith("segment/")
    assert f"Path: {path}" in prompt.detail_lines


def test_hostile_text_is_stripped_of_controls() -> None:
    prompt = _prompt(body={"values": [["a‮b​c"]]})
    assert "‮" not in (prompt.consequence or "") + "".join(prompt.detail_lines)
    assert "​" not in (prompt.consequence or "") + "".join(prompt.detail_lines)


def test_details_stay_within_a_bounded_size() -> None:
    prompt = _prompt(body={"values": [["x" * 100] * 1000]})
    assert sum(len(line) for line in prompt.detail_lines) < WRITE_BODY_LIMIT + 200
    assert "not shown" in prompt.detail_lines[-1], "an approver is told when Details is cut"


_FOLDER = "OneNote/" + "n" * 100


def test_a_folder_named_onenote_does_not_make_a_workbook_change_a_notebook_edit() -> None:
    """The reviewer's first path: the operative end stays visible and the title is the call's."""
    path = f"/me/drive/root:/{_FOLDER}/Budget.xlsx:/workbook/worksheets/Summary/delete"
    prompt = _prompt(method="POST", path=path)
    shown = prompt.consequence or ""
    assert prompt.title == "Change an Excel workbook in your Microsoft 365?"
    path_line = next(line for line in shown.splitlines() if line.startswith("Path:"))
    assert path_line.endswith("/workbook/worksheets/Summary/delete"), "the operative end shows"
    assert path_line.startswith("Path: /me/drive/root:/"), "the start shows too"
    assert "…" in path_line and len(path_line) <= len("Path: ") + 120
    assert f"Path: {path}" in prompt.detail_lines, "Details holds the whole path"


def test_a_folder_named_onenote_does_not_make_an_upload_a_notebook_edit() -> None:
    prompt = _prompt(
        method="PUT", path="/me/drive/root:/OneNote/Q3 Budget.xlsx:/content", file_handle="fh_1"
    )
    assert prompt.title == "Upload or replace a file in your Microsoft 365?"


def test_a_folder_named_workbook_does_not_make_an_upload_a_workbook_change() -> None:
    prompt = _prompt(
        method="PUT", path="/me/drive/root:/a/workbook/b.xlsx:/content", file_handle="fh_1"
    )
    assert prompt.title == "Upload or replace a file in your Microsoft 365?"


@pytest.mark.parametrize(
    ("method", "path", "title"),
    [
        ("PUT", "/me/drive/root:/Docs/a.docx:/content", "Upload or replace a file"),
        ("PUT", "/drives/d1/items/i1/content", "Upload or replace a file"),
        (
            "PATCH",
            "/v1.0/me/drive/items/i1/workbook/worksheets/S/range(address='A1')",
            "Change an Excel workbook",
        ),
        ("POST", "/me/drive/items/i1/workbook/tables/T/rows", "Change an Excel workbook"),
        ("POST", "/me/onenote/sections/s1/pages", "Add a OneNote page"),
        ("PATCH", "/me/onenote/pages/p1/content", "Change a OneNote page"),
        ("DELETE", "/me/drive/items/i1", "Change something"),
        ("POST", "/me/drive/root:/OneNote/x.docx:/children", "Change something"),
    ],
)
def test_the_title_follows_the_tools_own_classification(method: str, path: str, title: str) -> None:
    assert _prompt(method=method, path=path).title == f"{title} in your Microsoft 365?"


def test_a_body_at_the_limit_is_shown_whole_and_one_over_is_flagged() -> None:
    whole = _prompt(method="POST", path="/me/onenote/pages", body="a\n" * (WRITE_BODY_LIMIT // 2))
    assert not any("not shown" in line for line in whole.detail_lines)
    assert write_body_size("a\n" * (WRITE_BODY_LIMIT // 2)) == (WRITE_BODY_LIMIT,) * 2
    over = _prompt(method="POST", path="/me/onenote/pages", body="a" * (WRITE_BODY_LIMIT + 1))
    assert "(+1 more characters not shown)" in over.detail_lines[-1]


def test_wide_characters_are_cut_by_bytes_and_the_card_says_so() -> None:
    """Emoji cost 4 bytes a character on the card, so the byte limit binds before the other."""
    body = "😀" * (WRITE_BODY_BYTES // 4 + 10)
    prompt = _prompt(method="POST", path="/me/onenote/pages", body=body)
    shown = "".join(prompt.detail_lines[3:-1])
    assert len(shown.encode()) <= WRITE_BODY_BYTES
    assert "(+10 more characters not shown)" in prompt.detail_lines[-1]
