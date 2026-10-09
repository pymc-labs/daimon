"""The platform-neutral confirmation card and the in-process click rendezvous."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest
from daimon.core.confirmation import (
    CONFIRMATION_TIMEOUT,
    ConfirmationPrompt,
    PendingConfirmations,
    no_confirmation_surface,
    prompt_for_tool_call,
)
from daimon.core.posted_controls.confirmation import (
    build_confirmation_blocks,
    build_confirmation_card,
    confirmation_card_text,
    confirmation_custom_id,
    parse_confirmation_custom_id,
)
from daimon.core.tool_safety import ToolCall

_NOW = datetime(2026, 9, 28, 12, 0, tzinfo=UTC)


def _prompt() -> ConfirmationPrompt:
    call = ToolCall(
        tool_use_id="tu_1",
        server_name="linear",
        tool_name="create_issue",
        input={"title": "Bug", "team": "ENG"},
    )
    return prompt_for_tool_call(call, requester_platform_user_id="U1", now=_NOW)


def test_the_prompt_uses_plain_labels_without_tool_names() -> None:
    prompt = _prompt()
    assert prompt.title == 'Create issue "Bug"?'
    assert prompt.detail_lines == ("Title: Bug", "Team: ENG")
    assert prompt.expires_at == _NOW + CONFIRMATION_TIMEOUT


def test_a_long_generic_input_is_cut_to_a_short_line() -> None:
    call = ToolCall(
        tool_use_id="tu_1", server_name="s", tool_name="write_doc", input={"body": "x" * 5000}
    )
    prompt = prompt_for_tool_call(call, requester_platform_user_id="U1", now=_NOW)
    assert prompt.detail_lines[0].startswith("Body: ")
    assert prompt.detail_lines[0].endswith("…")
    assert len(prompt.detail_lines[0]) < 90


def test_a_naive_expiry_is_refused() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        ConfirmationPrompt(
            title="t", requester_platform_user_id="U1", expires_at=datetime(2026, 1, 1)
        )


@pytest.mark.parametrize(
    ("state", "mark"),
    [
        ("pending", "Create issue"),
        ("approved", "Approved"),
        ("denied", "Denied"),
        ("expired", "Expired"),
        ("stopped", "Stopped"),
    ],
)
def test_every_state_leads_with_its_mark(state: str, mark: str) -> None:
    token = "tok_abcdefgh" if state == "pending" else None
    card = build_confirmation_card(_prompt(), state=state, token=token)  # type: ignore[arg-type]
    assert card.headline.startswith(mark)
    assert card.detail_lines == (_prompt().detail_lines if state == "pending" else ())


def test_only_a_pending_card_carries_a_token() -> None:
    with pytest.raises(ValueError):
        build_confirmation_card(_prompt(), state="pending")
    with pytest.raises(ValueError):
        build_confirmation_card(_prompt(), state="approved", token="tok_abcdefgh")


def test_slack_blocks_carry_buttons_only_while_pending() -> None:
    prompt = _prompt()
    pending = build_confirmation_blocks(
        build_confirmation_card(prompt, state="pending", token="tok_abcdefgh"), prompt=prompt
    )
    actions = [b for b in pending if b["type"] == "actions"]
    assert [e["action_id"] for e in actions[0]["elements"]] == [
        "dcf:tok_abcdefgh:approve",
        "dcf:tok_abcdefgh:deny",
        "dcf:tok_abcdefgh:details",
    ]
    assert "<@U1>" in pending[-1]["elements"][0]["text"]
    assert pending[-1]["elements"][0]["text"].startswith("Only <@U1> can approve or deny\nExpires ")
    assert "Expires at <!date^" in pending[-1]["elements"][0]["text"]
    assert "^{time}|" in pending[-1]["elements"][0]["text"]
    assert "|12:10 UTC>" in pending[-1]["elements"][0]["text"]
    assert " · " not in str(pending)
    assert "linear" not in str(pending)
    assert "create_issue" not in str(pending)
    fallback = confirmation_card_text(
        build_confirmation_card(prompt, state="pending", token="tok_abcdefgh")
    )
    assert "linear" not in fallback and "create_issue" not in fallback
    assert " · " not in fallback
    approved = build_confirmation_blocks(
        build_confirmation_card(prompt, state="approved", answered_by_platform_user_id="U1"),
        prompt=prompt,
    )
    assert not [b for b in approved if b["type"] == "actions"]


def test_slack_tool_words_use_plain_text_blocks() -> None:
    value = "*x* `x` @everyone <@123456789012345678>"
    prompt = ConfirmationPrompt(
        title=f'Publish "{value}"?',
        consequence=f"Anyone with the link can open {value}.",
        detail_lines=(f"File: {value}",),
        requester_platform_user_id="U1",
        expires_at=_NOW,
    )
    card = build_confirmation_card(prompt, state="pending", token="tok_abcdefgh")
    blocks = build_confirmation_blocks(card, prompt=prompt)
    assert blocks[0]["text"] == {"type": "plain_text", "text": prompt.title}
    assert blocks[1]["text"] == {"type": "plain_text", "text": prompt.consequence}
    assert all(line not in str(blocks) for line in prompt.detail_lines)
    assert confirmation_card_text(card) == f"{prompt.title}\n{prompt.consequence}"


def test_button_ids_round_trip() -> None:
    assert parse_confirmation_custom_id(confirmation_custom_id("tok_abcdefgh", "approve")) == (
        "tok_abcdefgh",
        "approved",
    )
    assert parse_confirmation_custom_id(confirmation_custom_id("tok_abcdefgh", "deny")) == (
        "tok_abcdefgh",
        "denied",
    )
    assert parse_confirmation_custom_id("ztc:abc") is None


async def test_the_default_hook_refuses() -> None:
    assert await no_confirmation_surface(_prompt()) == "denied"


async def test_pending_confirmations_resolve_once() -> None:
    pending = PendingConfirmations()
    token, future = pending.open()
    waiter = asyncio.create_task(pending.wait(token, future, timeout_s=5))
    assert pending.resolve(token, "approved") is True
    assert pending.resolve(token, "denied") is False
    assert await waiter == "approved"


async def test_pending_confirmations_expire() -> None:
    pending = PendingConfirmations()
    token, future = pending.open()
    assert await pending.wait(token, future, timeout_s=0.01) == "expired"
    assert pending.resolve(token, "approved") is False


@pytest.mark.parametrize(
    ("tool", "input", "title", "consequence", "denied"),
    [
        (
            "create_notebook_upload_url",
            {"slug": "memecoin-scan"},
            'Publish notebook "memecoin-scan"?',
            "Anyone with the link can open it.",
            'Notebook "memecoin-scan" not published',
        ),
        (
            "create_notebook_upload_url",
            {"slug": "memecoin-scan", "editable": True},
            'Publish notebook "memecoin-scan"?',
            "Anyone with the link can open and edit it.",
            'Notebook "memecoin-scan" not published',
        ),
        (
            "create_notebook_upload_url",
            {},
            "Publish notebook?",
            "Anyone with the link can open it.",
            "Notebook not published",
        ),
        (
            "create_attachment_upload_url",
            {"name": "cg_meme.csv", "slug": "memecoin-scan"},
            'Upload "cg_meme.csv" to notebook "memecoin-scan"?',
            "Anyone with the notebook's link can open this file.",
            '"cg_meme.csv" not uploaded',
        ),
        (
            "publish_report",
            {"title": "Trends", "recipients": "team"},
            'Publish report "Trends"?',
            "Shared with team. Anyone with the link can open it.",
            'Report "Trends" not published',
        ),
        (
            "add_skill",
            {"name": "Research", "agent": "Daimon"},
            'Add skill "Research" to Daimon?',
            "This changes Daimon for everyone who uses it.",
            'Skill "Research" not added',
        ),
    ],
)
def test_approved_words_for_each_call_kind(
    tool: str, input: dict[str, object], title: str, consequence: str, denied: str
) -> None:
    from daimon.core.tool_safety import DAIMON_SERVER_NAME

    call = ToolCall(tool_use_id="tu", server_name=DAIMON_SERVER_NAME, tool_name=tool, input=input)
    prompt = prompt_for_tool_call(call, requester_platform_user_id="U1", now=_NOW)
    assert (prompt.title, prompt.consequence, prompt.denied_action) == (title, consequence, denied)
    assert "origin_context_id" not in "\n".join(prompt.detail_lines)


def test_details_hide_plumbing_but_keep_scope() -> None:
    call = ToolCall(
        tool_use_id="tu",
        server_name="daimon-mcp",
        tool_name="create_attachment_upload_url",
        input={"name": "cg_meme.csv", "slug": "memecoin-scan", "origin_context_id": "secret"},
    )
    prompt = prompt_for_tool_call(call, requester_platform_user_id="U1", now=_NOW)
    assert prompt.detail_lines == ("File: cg_meme.csv", "Notebook: memecoin-scan")


def test_details_use_plain_labels_for_publish_report_and_skill() -> None:
    from daimon.core.tool_safety import DAIMON_SERVER_NAME

    report = prompt_for_tool_call(
        ToolCall(
            tool_use_id="tu",
            server_name=DAIMON_SERVER_NAME,
            tool_name="publish_report",
            input={
                "title": "Q3 summary",
                "recipients": [{"name": "Alice", "label": "alice@x.com"}],
                "cap_usd": 5,
            },
        ),
        requester_platform_user_id="U1",
        now=_NOW,
    )
    assert report.detail_lines == (
        "Report: Q3 summary",
        "Shared with: alice@x.com",
        "Spending cap: $5",
    )
    skill = prompt_for_tool_call(
        ToolCall(
            tool_use_id="tu",
            server_name=DAIMON_SERVER_NAME,
            tool_name="add_skill",
            input={
                "agent_name": "Daimon",
                "path": "skills/notes",
                "repo_url": "https://example.com/repo",
            },
        ),
        requester_platform_user_id="U1",
        now=_NOW,
    )
    assert skill.detail_lines == ("Skill: notes", "Agent: Daimon", "Source: GitHub repo")


def test_generic_details_skip_plumbing_ids_nested_inputs_and_long_urls() -> None:
    prompt = prompt_for_tool_call(
        ToolCall(
            tool_use_id="tu-secret",
            server_name="linear",
            tool_name="create_issue",
            input={
                "title": "Bug",
                "team_id": "T123",
                "origin_context_id": "secret",
                "url": "https://example.com/" + "x" * 70,
                "metadata": {"private": "value"},
                "priority": 2,
                "note": "x" * 100,
            },
        ),
        requester_platform_user_id="U1",
        now=_NOW,
    )
    assert prompt.title == 'Create issue "Bug"?'
    assert prompt.detail_lines == ("Title: Bug", "Priority: 2", "Note: " + "x" * 79 + "…")
    shown = "\n".join((prompt.title, *prompt.detail_lines))
    assert "linear" not in shown and "tu-secret" not in shown and "secret" not in shown
    assert "{" not in shown and "https://" not in shown


async def test_a_slow_card_edit_returns_within_budget_and_still_lands() -> None:
    """Staging, 2026-10-09: two of six expiry edits timed out at 2s and were
    cancelled, so those cards kept live buttons. The turn must not wait past
    the budget, but the edit must still finish."""
    import asyncio

    from daimon.core.posted_controls.lifecycle import edit_card_within, pending_card_edits

    release = asyncio.Event()
    landed: list[str] = []

    async def slow_edit() -> None:
        await release.wait()
        landed.append("expired")

    loop = asyncio.get_running_loop()
    started = loop.time()
    await edit_card_within(
        slow_edit(),
        card_key="card-1",
        budget_s=0.05,
        failure_errors=(ValueError,),
        failed_event="test.edit_failed",
    )
    assert loop.time() - started < 1.0, "the turn waits no longer than the budget"
    assert landed == [] and pending_card_edits() == 1
    release.set()
    for _ in range(20):
        if landed:
            break
        await asyncio.sleep(0)
    assert landed == ["expired"], "the edit finishes in the background"
    await asyncio.sleep(0)  # the done-callback runs on the next loop pass
    assert pending_card_edits() == 0


async def test_a_card_edit_failure_inside_the_budget_is_logged_not_raised() -> None:
    from daimon.core.posted_controls.lifecycle import edit_card_within

    async def failing_edit() -> None:
        raise ValueError("gone")

    await edit_card_within(
        failing_edit(),
        card_key="card-2",
        budget_s=1.0,
        failure_errors=(ValueError,),
        failed_event="test.edit_failed",
    )


async def test_edits_to_one_card_land_in_call_order() -> None:
    """Review of #504: a slow Approved edit that finished after the Stopped
    edit following it left a refused card showing Approved."""
    import asyncio

    from daimon.core.posted_controls.lifecycle import edit_card_within, pending_card_edits

    release = asyncio.Event()
    landed: list[str] = []

    async def approved() -> None:
        await release.wait()
        landed.append("approved")

    async def stopped() -> None:
        landed.append("stopped")

    for edit in (approved(), stopped()):
        await edit_card_within(
            edit, card_key="card-3", budget_s=0.01, failure_errors=(ValueError,), failed_event="x"
        )
    assert landed == [], "the second edit waits for the first"
    release.set()
    for _ in range(50):
        if len(landed) == 2 and pending_card_edits() == 0:
            break
        await asyncio.sleep(0)
    assert landed == ["approved", "stopped"]


async def test_cancelling_the_caller_keeps_the_edit_tracked_and_landing() -> None:
    import asyncio
    import contextlib

    from daimon.core.posted_controls.lifecycle import edit_card_within, pending_card_edits

    release = asyncio.Event()
    landed: list[str] = []

    async def slow() -> None:
        await release.wait()
        landed.append("expired")

    caller = asyncio.create_task(
        edit_card_within(
            slow(), card_key="card-4", budget_s=10.0, failure_errors=(ValueError,), failed_event="x"
        )
    )
    await asyncio.sleep(0)
    caller.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await caller
    assert pending_card_edits() == 1, "the edit outlives its cancelled caller"
    release.set()
    for _ in range(50):
        if landed and pending_card_edits() == 0:
            break
        await asyncio.sleep(0)
    assert landed == ["expired"]
