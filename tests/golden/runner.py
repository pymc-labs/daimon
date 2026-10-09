"""Replay existing offline scenarios through the current integration path."""

from __future__ import annotations

import argparse
import difflib
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
GOLDENS = Path(__file__).resolve().parent
SCENARIOS = {
    "timer": "packages/core/tests/continuity/test_timers.py::test_a_fired_timer_dispatches_its_note_even_after_newer_messages",
    "env_mount_failure": "packages/core/tests/test_session_update_ops.py::test_env_replacement_clears_the_recorded_env_when_the_add_fails_after_the_delete",
    "plain_turn": "packages/core/tests/turn/test_driver.py::test_run_turn_folds_full_stream_and_returns_terminal_state",
    "tool_use": "packages/core/tests/turn/test_driver_policy_approval.py::test_chat_read_runs_without_a_card",
    "approval_card": "packages/core/tests/turn/test_driver_policy_approval.py::test_chat_write_shows_a_card_and_runs_only_after_confirm",
    "approval_card_discord": "tests/test_posted_controls_equivalence.py::test_confirmation_base_and_new[approve-discord]",
    "approval_card_slack": "tests/test_posted_controls_equivalence.py::test_confirmation_base_and_new[approve-slack]",
    "cancel_mid_stream": "packages/core/tests/turn/test_driver.py::test_interrupt_mid_consume_posts_user_interrupt_and_ends_clean_on_ack",
    "reconnect": "packages/core/tests/turn/test_driver_hooks.py::test_driver_calls_on_reconnect_on_connection_drop",
    "rate_limit": "packages/core/tests/turn/test_driver_hooks.py::test_driver_calls_on_rate_limited_with_until_before_sleep",
    "mcp_degraded": "packages/core/tests/turn/test_driver.py::test_mcp_failure_then_reply_finalizes_as_success_carrying_the_failure",
    "ceiling": "packages/core/tests/turn/test_driver_ceiling.py::test_past_deadline_returns_a_ceiling_turn_error",
    "billing_replay": "packages/core/tests/turn/test_driver_replay_billing.py::test_replayed_call_is_debited_with_the_turns_attribution",
    "plain_discord": "tests/parity/test_turn_billed.py::test_turn_billed_when_unblocked_writes_usage_event_and_ledger_debit[discord]",
    "plain_slack": "tests/parity/test_turn_billed.py::test_turn_billed_when_unblocked_writes_usage_event_and_ledger_debit[slack]",
    "blocked_balance": "tests/parity/test_turn_blocked_balance.py::test_turn_blocked_when_over_balance_writes_no_usage_and_no_ledger_row[discord]",
    "blocked_cap": "tests/parity/test_turn_blocked_cap.py::test_turn_blocked_when_over_cap_writes_no_new_usage_or_ledger_row[slack]",
    "dead_session": "tests/parity/test_dead_session.py::test_dead_session_recreates_marks_old_row_dead_and_bills_new_session[discord]",
    "handoff_full": "packages/core/tests/test_workspace_transfer.py::test_transfer_rehosts_the_bundle_and_enqueues_its_deletion",
    "handoff_transcript": "packages/core/tests/test_workspace_transfer.py::test_transfer_degrades_to_transcript_when_the_old_session_is_archived",
    "handoff_history": "packages/core/tests/test_workspace_transfer.py::test_transfer_returns_history_only_when_the_session_log_is_gone",
    "wake_continuation": "tests/parity/test_private_input_dispatch.py::test_private_input_continuation_dispatches_exactly_one_follow_up_turn[discord]",
    "handoff_continuation": "tests/parity/test_handoff_dispatch.py::test_handoff_continuation_dispatches_exactly_one_follow_up_turn[discord]",
    "dm_turn": "tests/integration/test_direct_messages.py::test_dm_scope_reuses_session_replays_history_bills_and_deduplicates[discord]",
    "sealed_channel": "packages/adapters/scheduler/tests/test_main.py::test_fire_stamps_a_sealed_destinations_seal_on_the_routine_session",
    "scheduler_run": "packages/adapters/scheduler/tests/test_main.py::test_fire_runs_in_the_routine_channels_environment[channel]",
    "mcp_start_turn": "packages/adapters/mcp/tests/tools/test_agent_chat.py::test_start_turn_returns_the_accepted_events_boundary",
    "mcp_continue_turn": "packages/adapters/mcp/tests/tools/test_agent_chat.py::test_continue_turn_returns_boundary_from_its_own_send",
    "mcp_cancel_turn": "packages/adapters/mcp/tests/tools/test_agent_chat.py::test_cancel_turn_sends_exactly_one_user_interrupt_event",
    "cli_session_get": "packages/adapters/cli/tests/commands/test_sessions.py::test_sessions_get_json_prints_session_body",
}


def replay(name: str) -> str:
    """One scenario per process prevents fixture/logging state leaking across goldens."""
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(filter(None, (str(GOLDENS), env.get("PYTHONPATH", ""))))
    with tempfile.TemporaryDirectory(prefix="daimon-oracle-") as scratch:
        output = Path(scratch) / "transcript.json"
        env["DAIMON_ORACLE_OUTPUT"] = str(output)
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                SCENARIOS[name],
                "-p",
                "oracle_plugin",
                "-q",
                "--tb=short",
            ],
            cwd=ROOT,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=120,
        )
        assert result.returncode == 0, f"Oracle scenario {name} failed:\n{result.stdout}"
        assert output.exists(), f"Oracle scenario {name} produced no transcript"
        return output.read_text()


def check(name: str, *, regen: bool = False) -> None:
    actual = replay(name)
    path = GOLDENS / f"{name}.json"
    if regen:
        path.write_text(actual)
        return
    expected = path.read_text()
    diff = "".join(
        difflib.unified_diff(
            expected.splitlines(True), actual.splitlines(True), fromfile=str(path), tofile="replay"
        )
    )
    assert actual == expected, f"Golden changed; only N3 may regenerate before M0.\n{diff}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--regen", action="store_true", help="N3-only: overwrite baseline recordings"
    )
    parser.add_argument("scenarios", nargs="*", choices=tuple(SCENARIOS))
    args = parser.parse_args()
    for name in args.scenarios or SCENARIOS:
        check(name, regen=args.regen)
        print(f"{name}: {'recorded' if args.regen else 'matched'}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
