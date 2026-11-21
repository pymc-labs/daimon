"""Every sentence of the workflows page and the workflow-authoring skill names the test that
backs it, or is listed as guidance that claims nothing (as `GITHUB_MCP_CLAIMS` does)."""

from __future__ import annotations

import ast
import json
import re
from collections.abc import Mapping
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
PAGES = (ROOT / "docs/workflows.md", ROOT / "defaults/skills/workflow-authoring/SKILL.md")
FIXTURES = ROOT / "web/src/modules/workflows/fixtures/validation.json"

_W = "packages/core/tests/workflows/"
_V = _W + "test_validate.py::test_validator_matches_shared_fixture"
_MCP = "packages/adapters/mcp/tests/tools/test_workflows.py::"
_WEB = "packages/adapters/web/tests/test_workflows_routes.py::"
_CLI = "packages/adapters/cli/tests/commands/test_workflows.py::"
_LANES = "packages/adapters/scheduler/tests/test_workflow_agent_lanes.py::"
_ASKS = "packages/adapters/{}/tests/test_workflow_asks.py::"
_GAPS = "tests/parity/test_workflow_chat_gaps.py::test_the_recorded_gaps_still_hold"
_OFF = _W + "test_features.py::test_workflow_features_default_off"
_EDIT = _MCP + "test_edit_workflow_draft_when_edits_add_and_connect_nodes_then_the_revision_moves"
_INBOX = _W + "test_waits.py::test_the_inbox_lists_and_decides_for_the_addressee"
_UNTRUSTED = (
    f"{_V}[untrusted input reaches an unbound write]",
    f"{_V}[a stranger's answer reaches a write]",
    f"{_V}[a write in a lane that saw the web]",
)
_STRANGERS = (
    _ASKS.format("discord")
    + "test_ask_reviewer_in_a_thread_refuses_strangers_and_leaves_chat_alone",
    _ASKS.format("slack") + "test_ask_reviewer_in_a_thread_refuses_strangers",
    _ASKS.format("teams") + "test_ask_reviewer_in_a_thread_refuses_strangers",
)

CLAIMS: Mapping[str, str | tuple[str, ...]] = {
    "is a graph of steps that runs on its own": (
        "packages/adapters/scheduler/tests/test_workflow_loops.py::"
        "test_a_manual_run_finishes_through_the_loops"
    ),
    "publish it as a numbered version": "packages/core/tests/stores/test_workflows.py::"
    "test_a_stale_draft_save_is_refused_and_publishing_numbers_versions",
    "tamper-evident record": _W + "test_engine.py::test_ledger_chain_verifies_and_detects_edits",
    "Workflows are Off by default.": _OFF,
    "An admin turns on": _OFF,
    "Webhook triggers and code steps are separate features": _OFF,
    "every path refuses": (
        _WEB + "test_every_workflows_route_refuses_when_the_feature_is_off",
        "packages/adapters/mcp/tests/test_capability_gate.py::"
        "test_every_capability_tool_when_its_feature_is_off_is_refused",
        _W + "test_chat_cards.py::test_every_chat_entry_refuses_while_workflows_are_off",
        _W + "test_ingress.py::test_limits_and_switches_refuse_before_anything_is_stored",
        _CLI + "test_every_command_refuses_while_workflows_are_off",
    ),
    "Runs already started stop": "packages/adapters/scheduler/tests/test_workflow_loops.py::"
    "test_the_loops_stop_runs_without_spending_while_workflows_are_off",
    "`${{ }}` expressions": _W
    + "test_expressions.py::test_render_keeps_the_type_of_a_lone_expression_and_joins_text",
    "steps before it.": f"{_V}[reads a later step]",
    "A loop needs": f"{_V}[a loop without max visits]",
    "a draft with errors cannot be published": (
        f"{_V}[unknown next step]",
        _W + "test_validate.py::test_a_draft_with_errors_cannot_be_published",
    ),
    "A workflow starts by hand": (
        _W + "test_runs.py::test_a_manual_run_goes_through_branch_transform_finish",
        _W + "test_triggers.py::test_a_schedule_fires_its_slot_once_and_starts_a_run",
        _W + "test_ingress.py::test_a_signed_delivery_starts_a_run_with_mapped_inputs_once",
        _W + "test_triggers.py::test_a_finished_run_starts_the_workflow_listening_for_it",
    ),
    "call it as a step": _W
    + "test_fan_out.py::test_a_workflow_step_waits_on_its_child_and_takes_its_output",
    "fires each slot once": _W
    + "test_triggers.py::test_a_schedule_fires_its_slot_once_and_starts_a_run",
    "A missed slot is skipped": _W
    + "test_triggers.py::test_a_missed_slot_is_skipped_unless_catch_up_runs_once",
    "checks the sender's signature": (
        _W
        + "test_signatures.py::test_standard_webhooks_reference_vector_verifies_and_tampering_fails",
        _W + "test_signatures.py::test_github_documented_vector_verifies",
        _W + "test_signatures.py::test_stripe_signature_verifies_any_v1_and_refuses_stale",
        _W + "test_signatures.py::test_slack_documented_vector_verifies_within_five_minutes",
        _W + "test_signatures.py::test_bearer_compares_the_token",
    ),
    "Each delivery starts at most one run.": _W
    + "test_ingress.py::test_a_signed_delivery_starts_a_run_with_mapped_inputs_once",
    "test URL": _W
    + "test_ingress.py::test_a_listening_test_url_captures_one_delivery_as_a_test_run",
    "step asks people": (
        _ASKS.format("slack") + "test_ask_derick_by_dm_resolves_in_the_thread_under_it",
        *_STRANGERS,
        _INBOX,
    ),
    "On Discord, the person answers by reply": (
        _ASKS.format("discord") + "test_ask_derick_by_dm_resolves_by_reply_and_closes_the_ask",
        _ASKS.format("slack") + "test_ask_derick_by_dm_resolves_in_the_thread_under_it",
        _ASKS.format("teams") + "test_ask_derick_by_dm_resolves_by_its_button_and_closes_the_card",
    ),
    "someone who was not asked is refused": _STRANGERS,
    "An answer can be approve or reject": (
        _W + "test_waits.py::test_reject_says_why_and_fails_without_a_reject_route",
        f"{_V}[a choice with one route]",
        _W + "test_waits.py::test_text_answers_become_the_output",
    ),
    "Several approvers count as distinct people": _W
    + "test_waits.py::test_n_of_m_counts_distinct_people_and_refuses_self_approval",
    "times out to its own route": _W
    + "test_waits.py::test_timers_arrive_and_approvals_time_out_to_their_route",
    "An agent step can also ask a question.": _W
    + "test_agent_step.py::test_a_question_parks_the_step_and_the_answer_resumes_its_session",
    "the answer resumes the agent's session": _W
    + "test_agent_step.py::test_a_question_parks_the_step_and_the_answer_resumes_its_session",
    "on a canvas from a blank draft or a starter": (
        _WEB + "test_a_blank_workflow_is_drafted_published_and_run_to_the_end",
        _WEB + "test_each_starter_template_creates_a_draft_that_validates",
    ),
    "Ask Daimon, beside the canvas": (
        _WEB + "test_ask_daimon_message_when_a_node_is_selected_names_the_workflow_to_the_agent",
        "packages/core/tests/test_turn_origin.py::"
        "test_render_turn_origin_when_a_workflow_is_open_names_it_and_the_selected_node",
    ),
    "A private workflow is hidden": _WEB
    + "test_a_private_workflow_is_hidden_from_a_member_until_it_is_shared",
    "opens the workflows panel": "tests/parity/test_workflow_chat_gaps.py::"
    "test_every_chat_platform_has_the_workflows_panel",
    "the agent does not publish or start a live run itself": _MCP
    + "test_in_a_chat_turn_publish_and_runs_answer_with_cards",
    "It posts a card for you to press": _MCP
    + "test_in_a_chat_turn_publish_and_runs_answer_with_cards",
    "shows on a run card that follows it": _W
    + "test_chat_cards.py::test_a_confirmed_run_starts_on_its_card_and_the_card_follows_it",
    "A Finish step can post the result": (
        _W + "test_delivery.py::test_targets_name_threads_on_every_platform",
        _W + "test_delivery.py::test_post_time_rules_and_dms",
    ),
    "one card for each streak of failures": _W
    + "test_delivery.py::test_the_owner_is_told_once_per_streak_of_failures",
    "sees outside input must not also write": _UNTRUSTED[0],
    "Outside input is": _UNTRUSTED,
    "unless an Approval before it binds the write": f"{_V}[binds an action after an agent]",
    "unless an approval binds the write": (f"{_V}[binds an action after an agent]", *_UNTRUSTED),
    "sends exactly what was approved": _W
    + "test_actions.py::test_3_inbound_lead_sends_exactly_the_approved_email",
    "propose its writes sends only": _W
    + "test_actions.py::test_approved_proposals_are_sent_and_the_rest_are_not",
    "`writes: propose`": _W
    + "test_actions.py::test_approved_proposals_are_sent_and_the_rest_are_not",
    "test run never writes to real systems": _W
    + "test_actions.py::test_one_rule_decides_every_unattended_call",
    "Some settings need an admin's approval": (
        f"{_V}[a waived agent that writes]",
        f"{_V}[an unverified webhook]",
        f"{_V}[a budget over the default]",
        f"{_V}[agents per item at high concurrency with built-ins]",
    ),
    "the grant carries over to later versions": (
        _W + "test_waits.py::test_an_admin_approves_a_publish_request_from_the_inbox",
        _W + "test_actions.py::test_a_write_grant_is_requested_approved_and_carried_over",
    ),
    "You can cancel a run": (
        _W + "test_runs.py::test_cancel_makes_the_running_settle_stale",
        _W + "test_runs.py::test_redrive_reruns_the_failed_step_on_the_same_run",
        _W + "test_runs.py::test_fork_copies_upstream_outputs_and_starts_at_the_node",
    ),
    "active limit queue": _W + "test_runs.py::test_runs_over_the_active_limit_queue",
    "passes its deadline or spends its budget": _W
    + "test_runs.py::test_deadline_and_budget_end_the_run",
    "Each step's spend counts": _LANES
    + "test_step_spend_counts_against_the_run_as_persons_allowance_and_gates_them",
    "move a routine to a workflow and back": (
        _WEB + "test_an_admin_moves_a_routine_to_a_workflow_and_back",
        _CLI + "test_from_routine_previews_without_moving_then_moves_and_moves_back",
    ),
    "takes over the routine's schedule": (
        _W + "test_convert.py::test_a_routine_moves_to_a_workflow_and_back_with_each_slot_once",
        _W + "test_convert.py::test_a_paused_routine_moves_to_a_paused_workflow",
    ),
    "posts to its owner's direct messages": (
        _W + "test_convert.py::test_a_dm_routine_becomes_an_owner_dm_step_that_posts_to_the_owner",
        _LANES + "test_an_owner_dm_step_runs_as_the_owners_dm_and_skips_when_they_are_busy",
    ),
    "or already moved, stays": _W + "test_convert.py::test_routines_that_cannot_move_are_refused",
    "commands act as a workspace admin": _CLI
    + "test_a_file_imports_as_a_draft_lists_shows_and_exports_the_same_spec",
    "An import arrives as a draft": (
        _CLI + "test_a_file_imports_as_a_draft_lists_shows_and_exports_the_same_spec",
        _CLI + "test_import_refuses_an_owner_who_is_not_on_the_tenant",
    ),
    "checks a run's record": _CLI + "test_runs_are_listed_redriven_cancelled_and_verified",
    "only previews until": _CLI
    + "test_from_routine_previews_without_moving_then_moves_and_moves_back",
    "only on the Anthropic backend": _W
    + "test_runs.py::test_a_home_channel_on_another_backend_refuses_the_run",
    "A run form in chat holds": _GAPS,
    "Longer forms open in the web app.": _GAPS,
    "A draft card does not refresh": _GAPS,
    "Publishing from chat records no note.": _GAPS,
    "a run confirmation card cannot start a run": _GAPS,
    "replaces the pressed card": _GAPS,
    # The skill.
    "nothing goes live until someone publishes it": _EDIT,
    "passing that revision as `base_revision`": _EDIT,
    "Edits apply in order": _EDIT,
    "you are told its new revision": _MCP
    + "test_edit_workflow_draft_when_the_revision_is_stale_names_the_current_one",
    "lists errors, warnings and grants": _EDIT,
    "A test run uses the draft and cannot write": (
        _W + "test_runs.py::test_test_runs_use_the_draft_and_pinned_outputs",
        _W + "test_actions.py::test_one_rule_decides_every_unattended_call",
    ),
    "An agent key cannot publish.": _MCP
    + "test_publish_workflow_when_an_agent_key_asks_is_refused_for_a_person",
    "Every path ends at a `finish`.": (f"{_V}[no finish step]", f"{_V}[a step with no next step]"),
    "needs a `has()` guard": (
        f"{_V}[reads a branch arm that may be skipped]",
        f"{_V}[guards a branch arm with has()]",
    ),
    "A branch condition is one whole expression.": f"{_V}[a branch condition without markers]",
    "take no expressions": f"{_V}[instructions with an expression]",
}

#: Sentences that direct the reader or point elsewhere; they claim no behaviour.
GUIDANCE = (
    "How workflows run, how people take part",
    "Deployment caps on steps, runs, items and webhooks are under",
    "Name the workspace with `--tenant`.",
    "Read it with `get_workflow`.",
    "Note `revision`.",
    "Read it again and redo the edit.",
    "Fix the problems that come back.",
    "Try it with `run_workflow` and `test: true`.",
    "Publish only when the person asks.",
)


def _sentences(text: str) -> list[str]:
    """Prose only: no headings, tables, code blocks or frontmatter; list items count."""
    lines: list[str] = []
    code = False
    for line in text.splitlines():
        if line.startswith("```"):
            code = not code
        elif not code and not line.startswith(("#", "|", "---", "name:", "description:")):
            lines.append(re.sub(r"^\s*(-|\d+\.)\s+", "", line))
    flat = " ".join(" ".join(lines).split())
    return [part for part in re.split(r"(?<=[.!?])\s+", flat) if part]


def _test_exists(node_id: str) -> bool:
    path, _, name = node_id.partition("::")
    file = ROOT / path
    name, _, case = name.partition("[")
    if not file.is_file():
        return False
    tree = ast.parse(file.read_text())
    found = any(
        isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == name
        for node in tree.body
    )
    cases = {c["name"] for c in json.loads(FIXTURES.read_text())["cases"]}
    return found and (not case or case.removesuffix("]") in cases)


def test_every_workflows_claim_names_a_backing_test() -> None:
    sentences = [s for page in PAGES for s in _sentences(page.read_text())]
    unmapped = [s for s in sentences if not any(k in s for k in (*CLAIMS, *GUIDANCE))]
    assert not unmapped, f"map these sentences to the test that backs them: {unmapped}"
    for key in (*CLAIMS, *GUIDANCE):
        assert any(key in s for s in sentences), f"stale claim key: {key!r}"
    for key, backing in CLAIMS.items():
        for node_id in (backing,) if isinstance(backing, str) else backing:
            assert _test_exists(node_id), f"{node_id} backs {key!r} but does not exist"
