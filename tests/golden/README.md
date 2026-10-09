# Neutral-core current-path oracle

These 33 offline scenarios run existing tests and four boundary scenarios against the production path from integration `546ed15c7765d270a228beb730258002dbe09e98`. No production module changed while recording. The stack is now rebased onto integration `4d61c7391a5098f8ae1cffd7ca1a80fb077af326` (#488); its main-merge timing changes are reviewed in the timing audit, and baseline transcripts remain subject to full replay on that base. The pytest test in `tests/parity/test_ma_goldens.py` replays every scenario and compares its entire transcript. Failure of the original scenario test is also a failure of the oracle.

Configure `DAIMON_DATABASE__TEST_URL` for an isolated, migrated local test database before running. N3 uses `daimon_test_nc_n3`. Do not use the shared `daimon_test` or live provider credentials.

Fixture setup obtains mapped metadata through the public `daimon.testing.effect_recorder.database_metadata()` helper. It pins defaults on the actual mapped tables, including Python UUID/time defaults that database reflection would omit, without importing private core models from the test tree.

```bash
uv run pytest -n 2 -q tests/parity/test_ma_goldens.py tests/parity/test_ma_call_ratchet.py
uv run python tests/golden/runner.py plain_discord approval_card
# Only N3 may record from unchanged integration; forbidden in extraction lanes.
uv run python tests/golden/runner.py --regen
```

The runner starts one fresh pytest process per scenario. The instrumentation observes the real SDK over existing MockTransport/MARouter/stateful resource fakes and ScriptedTransport, existing Discord/Slack fake clients, lifecycle callbacks, confirmation prompt/card posts and edits, CLI JSON output, DM turns, and scheduler execution/billing bindings. It captures all columns in `usage_events`, `tenant_ledger`, `turn_outcomes`, `thread_sessions`, and `task_continuations`, including rows still in the test fixture's transaction, after draining background writes. No domain rules are copied into a new scenario implementation.

Requests and platform effects keep their original order; database rowsets sort by semantic identity before normalizing runtime PKs. Money, caller/account/tenant identities, model names, errors and continuity data remain literal. The application clock, SQL timestamp defaults and SQL now()/current_timestamp expressions are fixed before fixtures run. Observed timestamps retain whole-second offsets from the frozen epoch; scheduled timestamps retain whole-second offsets from their record anchor (or explicit scenario epoch). This golden-specific normalizer preserves ledger dating as well as timer/expiry durations. PR2’s real-clock recorder still normalizes observed times opaquely for commit jitter. Cold billed turns have provider processed_at at epoch −90s, distinct from application receipt time at epoch zero. UUID and fake resource token fixtures are pinned before collection as well as fixture setup; wall time and adapter display clocks are fixed, and fake channel names are concrete strings, so literal caller IDs stay stable. JSON request bodies stay readable; multipart bodies retain filenames, field order and exact bytes without their random wire boundary. Serialization errors and swallowed MA script assertions fail recording. The dead-session fixture has an explicit scripted 404 for the expired session’s history endpoint, which its existing router leaves unregistered; this uses PR2’s ScriptedTransport rather than treating an unscripted request as a connection error. The existing Discord sent-message receipt also gets its missing async reaction method, so feedback reactions are captured instead of producing a fixture-only TypeError.

The scenario clock does not advance periodic render ticks. Forced final renders, SSE/lifecycle order, scripted retry behavior and existing scenario assertions still run. Short cancellation/approval triggers wait on actual stream/card events. This oracle therefore records deterministic terminal rendering; the existing turn tests cover periodic rendering separately. Turn duration uses a frozen observation clock. Existing scenario fakes choose their SDK retry policy; these fixtures do not certify production's eight-retry budget or wall-clock latency. PR2's scripted client supports `max_retries=8` for explicit retry scripts.

DM core coverage runs two admitted and billed private turns through real core/HTTP fakes, reuses the session, replays history, and deduplicates a repeated delivery. The additional `dm_delivery` scenario drives the actual Discord DM listener through real core/HTTP fakes and records both reply payloads and mention suppression at the platform boundary; repeated delivery produces no third reply. Scheduler goldens capture the host's existing run-turn boundary and exact billing bindings; their existing tests replace session execution with a fake. Billed Discord/Slack goldens exercise full current turn execution and real ledger/outcome writes. The approval platform cases reuse the existing posted-controls equivalence tests; instrumentation records the current adapter's card path while the tests independently compare it with the frozen pre-refactor handler.

The nine turn fixture scenarios (`plain_turn`, `tool_use`, `approval_card`, `cancel_mid_stream`, `reconnect`, `rate_limit`, `mcp_degraded`, `ceiling`, `billing_replay`) now use their existing scripts behind real SDK HTTP serialization and SSE parsing. `http_turn.py` preserves original fake counters, interruptions and source assertions, while ScriptedTransport records request body/query/beta headers. Method-level FakeEventsResource records are replaced by HTTP records so future drivers can share the parity boundary. The ceiling scenario waits for the real SDK HTTP stream’s first body read before advancing a fake deadline clock and cancelling/draining the production pump. The old millisecond wall-clock deadline raced SDK setup on unchanged code. This controlled timeout retains exact deadline calculation, ceiling classification/message, initial-send, stream closure and task-cleanup assertions while making the HTTP order repeatable. The original driver ceiling tests still cover real asyncio deadlines. Run the explicit stress target with `uv run pytest -n 2 -q tests/golden/test_ceiling_stress.py`: it checks the complete ceiling and cancellation transcripts twenty times each across two workers; it is outside default collection. The cancellation fixture’s original 20ms sleep similarly raced SDK setup, so its timer now waits until the first SSE event was consumed before interrupting. Its original partial-content and clean-ack assertions still run unchanged.

Slack Web API capture includes params (reaction name, channel/timestamp, users.info caller and history targeting) and safe content-type/accept headers. Authorization is excluded. Cold-thread Discord/Slack scenarios enable completion notifications and create their session through actual core SDK calls. **Current order is deliberately frozen:** Slack eyes reaction and status post precede POST /v1/sessions; Discord status post precedes session creation, while 👀 follows POST initial events because the driver acknowledges accepted delivery after that POST succeeds. Completion reactions and removals are also recorded. Moving Discord 👀 before creation would change current behavior and is outside oracle recording; the lead confirmed the existing order.

Four checked-in production-code mutation probes run in isolated child processes and require their source scenario to pass but their complete golden transcript to differ: Slack eyes → hourglass, Discord 👀 → 👁, ledger occurred_at=event.processed_at → None, and altered DM reply text. They edit only loaded function code and restore it at exit; production files remain unchanged. Normal golden pytest collection runs all four. To inspect an expected failure directly:

```bash
uv run python tests/golden/runner.py plain_slack --mutation slack_eyes
uv run python tests/golden/runner.py cold_discord --mutation discord_eyes
uv run python tests/golden/runner.py cold_discord --mutation ledger_dating
uv run python tests/golden/runner.py dm_delivery --mutation dm_delivery
```

Opaque dependency objects and credential-bearing scheduler configuration use fixture-type markers; credentials are never recorded. Billing callback bindings include exact markup/pricing and caller/channel attribution. No paid API calls are needed. The [timing audit](TIMING-AUDIT.md) covers all 33 scenarios and documents the explicit five-pass regeneration proof under two-worker load. Approval-card polling now waits on the real confirmation callback instead of its original one-second cap; the source assertions are unchanged.

| Scenario | Existing offline test |
| --- | --- |
| `timer` | `packages/core/tests/continuity/test_timers.py::test_a_fired_timer_dispatches_its_note_even_after_newer_messages` |
| `env_mount_failure` | `packages/core/tests/test_session_update_ops.py::test_env_replacement_clears_the_recorded_env_when_the_add_fails_after_the_delete` |
| `plain_turn` | `packages/core/tests/turn/test_driver.py::test_run_turn_folds_full_stream_and_returns_terminal_state` |
| `tool_use` | `packages/core/tests/turn/test_driver_policy_approval.py::test_chat_read_runs_without_a_card` |
| `approval_card` | `packages/core/tests/turn/test_driver_policy_approval.py::test_chat_write_shows_a_card_and_runs_only_after_confirm` |
| `approval_card_discord` | `tests/test_posted_controls_equivalence.py::test_confirmation_base_and_new[approve-discord]` |
| `approval_card_slack` | `tests/test_posted_controls_equivalence.py::test_confirmation_base_and_new[approve-slack]` |
| `cancel_mid_stream` | `packages/core/tests/turn/test_driver.py::test_interrupt_mid_consume_posts_user_interrupt_and_ends_clean_on_ack` |
| `reconnect` | `packages/core/tests/turn/test_driver_hooks.py::test_driver_calls_on_reconnect_on_connection_drop` |
| `rate_limit` | `packages/core/tests/turn/test_driver_hooks.py::test_driver_calls_on_rate_limited_with_until_before_sleep` |
| `mcp_degraded` | `packages/core/tests/turn/test_driver.py::test_mcp_failure_then_reply_finalizes_as_success_carrying_the_failure` |
| `ceiling` | `tests/golden/test_boundary_scenarios.py::test_ceiling_after_http_setup_closes_stream` |
| `billing_replay` | `packages/core/tests/turn/test_driver_replay_billing.py::test_replayed_call_is_debited_with_the_turns_attribution` |
| `cold_discord` | `tests/golden/test_boundary_scenarios.py::test_cold_thread_feedback_before_session_create[discord]` |
| `cold_slack` | `tests/golden/test_boundary_scenarios.py::test_cold_thread_feedback_before_session_create[slack]` |
| `dm_delivery` | `tests/golden/test_boundary_scenarios.py::test_dm_reply_delivery_and_deduplication` |
| `plain_discord` | `tests/parity/test_turn_billed.py::test_turn_billed_when_unblocked_writes_usage_event_and_ledger_debit[discord]` |
| `plain_slack` | `tests/parity/test_turn_billed.py::test_turn_billed_when_unblocked_writes_usage_event_and_ledger_debit[slack]` |
| `blocked_balance` | `tests/parity/test_turn_blocked_balance.py::test_turn_blocked_when_over_balance_writes_no_usage_and_no_ledger_row[discord]` |
| `blocked_cap` | `tests/parity/test_turn_blocked_cap.py::test_turn_blocked_when_over_cap_writes_no_new_usage_or_ledger_row[slack]` |
| `dead_session` | `tests/parity/test_dead_session.py::test_dead_session_recreates_marks_old_row_dead_and_bills_new_session[discord]` |
| `handoff_full` | `packages/core/tests/test_workspace_transfer.py::test_transfer_rehosts_the_bundle_and_enqueues_its_deletion` |
| `handoff_transcript` | `packages/core/tests/test_workspace_transfer.py::test_transfer_degrades_to_transcript_when_the_old_session_is_archived` |
| `handoff_history` | `packages/core/tests/test_workspace_transfer.py::test_transfer_returns_history_only_when_the_session_log_is_gone` |
| `wake_continuation` | `tests/parity/test_private_input_dispatch.py::test_private_input_continuation_dispatches_exactly_one_follow_up_turn[discord]` |
| `handoff_continuation` | `tests/parity/test_handoff_dispatch.py::test_handoff_continuation_dispatches_exactly_one_follow_up_turn[discord]` |
| `dm_turn` | `tests/integration/test_direct_messages.py::test_dm_scope_reuses_session_replays_history_bills_and_deduplicates[discord]` |
| `sealed_channel` | `packages/adapters/scheduler/tests/test_main.py::test_fire_stamps_a_sealed_destinations_seal_on_the_routine_session` |
| `scheduler_run` | `packages/adapters/scheduler/tests/test_main.py::test_fire_runs_in_the_routine_channels_environment[channel]` |
| `mcp_start_turn` | `packages/adapters/mcp/tests/tools/test_agent_chat.py::test_start_turn_returns_the_accepted_events_boundary` |
| `mcp_continue_turn` | `packages/adapters/mcp/tests/tools/test_agent_chat.py::test_continue_turn_returns_boundary_from_its_own_send` |
| `mcp_cancel_turn` | `packages/adapters/mcp/tests/tools/test_agent_chat.py::test_cancel_turn_sends_exactly_one_user_interrupt_event` |
| `cli_session_get` | `packages/adapters/cli/tests/commands/test_sessions.py::test_sessions_get_json_prints_session_body` |
