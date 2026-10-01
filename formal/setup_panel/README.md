# Discord setup panel Details state

Run the setup-panel and coding-tools identity configurations with the repository's pinned TLC tool:

```sh
formal/check.sh
```

`SetupPanelPreFix.cfg` and `SetupPanelPreFixCoding.cfg` preserve counterexamples
for setup-thread targeting and coding-tool token targeting. The safe
configuration checks that the latest roster click alone may publish Details,
and that both actions use the target captured by that rendered Details view.

`CodingTokenIdentity.tla` models the later MA lookup performed when a coding-
tools token is minted. Its actions render A's card, add newer same-name B after
that render, optionally invalidate A, then mint. `CodingTokenIdentityNameSubstitution.cfg`
retains the counterexample where the name resolver selects B. The safe
configurations retrieve the card's exact ID; if A is archived, missing, or
foreign-tenant by click time, they mint no token.

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
| Coding-tools token uses the card's captured MA ID and validates it is live and tenant-owned | [`mcp_access.py`](../../packages/adapters/discord/daimon/adapters/discord/agent_setup/mcp_access.py#L84) and [`get_setup_agent`](../../packages/core/daimon/core/setup_conversations.py#L55) |
| New-agent modal does not replace a newer panel | [`new_agent.py`](../../packages/adapters/discord/daimon/adapters/discord/agent_setup/new_agent.py#L62) |
| Both controlled completion orders and captured actions | [`test_roster_view.py`](../../packages/adapters/discord/tests/agent_setup/test_roster_view.py#L603), [`test_details_view.py`](../../packages/adapters/discord/tests/agent_setup/test_details_view.py#L701), and [`test_mcp_access.py`](../../packages/adapters/discord/tests/agent_setup/test_mcp_access.py#L203) |

## Coding-tools identity model

| Model item | Implementation / assumption |
| --- | --- |
| Card snapshot holds an immutable MA ID | `RosterAgent.ma_agent_id`, passed by `DetailsView._on_coding_tools` to `send_coding_tools_access` |
| Unsafe name resolver selects the newest tagged match | `find_agents_by_daimon_tag` sorts matching `(tenant, name)` rows by `created_at` descending in [`ma_index.py`](../../packages/core/daimon/core/defaults/ma_index.py#L57) |
| Safe mint retrieves the exact selected ID, then checks live status and tenant metadata | [`get_setup_agent`](../../packages/core/daimon/core/setup_conversations.py#L55) |
| Mint writes the derived ID and caller tenant | [`mcp_access.py`](../../packages/adapters/discord/daimon/adapters/discord/agent_setup/mcp_access.py#L96) and [`mcp_auth.py`](../../packages/core/daimon/core/mcp_auth.py#L103) |
| Executable duplicate, archive, foreign-tenant and missing-ID cases | [`test_mcp_access.py`](../../packages/adapters/discord/tests/agent_setup/test_mcp_access.py#L203) |

The bounded identity model treats MA retrieve as one atomic result and models
the current tenant as the tenant resolved for the Discord interaction. The
unsafe resolver candidate is already filtered to that tenant and is the newest
same-name match. Thus its counterexample is an identity mismatch within one
tenant, not cross-tenant access. The safe action emits a token only when the
card ID exists, is live, and carries the current tenant marker. MA request
failures other than not-found, database failures during token creation, and
later changes to the agent after mint remain outside this model. `InvalidateCard`
is constrained to before mint because the model checks the identity at issuance;
post-mint archive handling and token revocation are separate behavior. The
finite bound is two MA agents, one shared name, and two tenant values. This is a
safety check only; no fairness or progress claim is made after rendering.

| Model action / variable | Source mapping |
| --- | --- |
| `RenderCard` sets `cardAgent` to A | [`load_roster`](../../packages/core/daimon/core/roster.py#L122) and [`roster_view.py`](../../packages/adapters/discord/daimon/adapters/discord/agent_setup/roster_view.py#L260) |
| `ReplaceOrDuplicateName` makes B the newer matching name | [`find_agents_by_daimon_tag`](../../packages/core/daimon/core/defaults/ma_index.py#L57), which sorts matches newest-first |
| Unsafe `Mint` chooses B by name | Pre-fix [`mcp_access.py`](../../packages/adapters/discord/daimon/adapters/discord/agent_setup/mcp_access.py#L84) lookup by `selected.name` |
| Safe `Mint` keeps the card ID and requires live, tenant-owned identity | [`get_setup_agent`](../../packages/core/daimon/core/setup_conversations.py#L55), then [`mcp_access.py`](../../packages/adapters/discord/daimon/adapters/discord/agent_setup/mcp_access.py#L84) |
| `InvalidateCard` models deletion, archive, or tenant metadata mismatch before mint | [`get_setup_agent`](../../packages/core/daimon/core/setup_conversations.py#L60)–[`L73`](../../packages/core/daimon/core/setup_conversations.py#L73) |

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
- Tenant authorization beyond the exact-ID tenant metadata check, MA/DB read
  semantics, modal submission, setup thread persistence, process restart,
  Discord library dispatch internals, and Slack view-stack behavior are
  excluded. Their runtime paths are not proven by TLC.
- These finite configurations check the modeled interleavings; they do not
  prove all Python schedules or remote platform behavior.
