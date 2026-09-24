# Discord setup panel Details state

Run the three configurations with the repository's pinned TLC tool:

```sh
formal/check.sh
```

`SetupPanelPreFix.cfg` and `SetupPanelPreFixCoding.cfg` preserve counterexamples
for setup-thread targeting and coding-tool token targeting. The safe
configuration checks that the latest roster click alone may publish Details,
and that both actions use the target captured by that rendered Details view.

The setup-target counterexample follows the executable race:

1. Click Details for agent A; its read remains pending.
2. Click Details for agent B; shared selection becomes B.
3. A's read completes and renders details for A while its action still reads B.
4. Click Setup; the conversation targets B although the card describes A.

The coding-tools configuration reaches the same completion state and then
invokes the token action; it records B as the target for the A card. In the safe
configuration, the request sequence makes B the only callback allowed to
render. If A completes after B renders, A is stale and is discarded. If another
panel screen renders while a Details read is pending, the render-generation
check also discards that result.

| Model item | Implementation |
| --- | --- |
| Per-click request sequence and latest-read fence | [`roster_view.py`](../../packages/adapters/discord/daimon/adapters/discord/agent_setup/roster_view.py#L260) and [`state.py`](../../packages/adapters/discord/daimon/adapters/discord/agent_setup/state.py#L108) |
| Origin render-generation check and stale-view rejection | [`navigation.py`](../../packages/adapters/discord/daimon/adapters/discord/agent_setup/navigation.py#L49) and [`expiry.py`](../../packages/adapters/discord/daimon/adapters/discord/agent_setup/expiry.py#L112) |
| Immutable Details and target snapshot, including toggles | [`details_view.py`](../../packages/adapters/discord/daimon/adapters/discord/agent_setup/details_view.py#L280) |
| Setup uses the card's captured agent | [`details_view.py`](../../packages/adapters/discord/daimon/adapters/discord/agent_setup/details_view.py#L340) |
| Coding-tools token uses the card's captured agent | [`mcp_access.py`](../../packages/adapters/discord/daimon/adapters/discord/agent_setup/mcp_access.py#L48) |
| New-agent modal does not replace a newer panel | [`new_agent.py`](../../packages/adapters/discord/daimon/adapters/discord/agent_setup/new_agent.py#L62) |
| Both controlled completion orders and captured actions | [`test_roster_view.py`](../../packages/adapters/discord/tests/agent_setup/test_roster_view.py#L603) and [`test_details_view.py`](../../packages/adapters/discord/tests/agent_setup/test_details_view.py#L701) |

## Bounds and assumptions

- Two agents, A and B, and at most one click per agent from the same rendered
  roster. Both clicks begin before the first Details response renders, which is
  the race where both controls can still be on screen.
- The model checks safety only. It makes no fairness or progress claim about
  whether either Details read completes or whether a user action eventually
  reaches the platform.
- Each completed read and the synchronous state assignment, `DetailsView`
  construction, and render-generation increment are one model action. The
  production code has no `await` between those operations. A subsequent
  Discord REST edit is abstracted as completing with that render action; the
  model does not explore API failure or timeouts.
- `AdvancePanel` stands for any other panel render or expiry while the reads
  are pending. It advances the generation once; the safe model requires the
  originating generation to match before a callback publishes.
- `OpenSetup` and `MintCodingTools` record both the card's details identity and
  the resulting target. This records each action's own card snapshot, so a
  later legitimate panel render does not invalidate an earlier action.
- Tenant authorization, MA/DB read semantics, modal submission, setup thread
  persistence, process restart, Discord library dispatch internals, and Slack
  view-stack behavior are excluded. Their runtime paths are not proven by TLC.
- These finite configurations check the modeled interleavings; they do not
  prove all Python schedules or remote platform behavior.
