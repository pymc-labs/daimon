# OAuth installation and single-use authorization callbacks

Three small models cover the OAuth boundary listed as unmodelled P1 in the
[coverage table](../README.md):

| Model | What it abstracts | Sources |
| --- | --- | --- |
| `McpOAuthFlow.tla` | One `mcp_oauth_flows` row: two opens of the same start link racing discovery, client registration and `save_flow_client`; each browser's callback (approve or decline), possibly delivered twice; `consume_flow`; the code exchange with the row's client; `mark_flow_completed`. | [`mcp_oauth_flows.py`](../../packages/core/daimon/core/stores/mcp_oauth_flows.py), [`handshake.py`](../../packages/core/daimon/core/mcp_oauth/handshake.py), [`complete.py`](../../packages/core/daimon/core/mcp_oauth/complete.py), [`oauth_mcp.py`](../../packages/adapters/mcp/daimon/adapters/mcp/oauth_mcp.py) |
| `VaultSlot.tla` | One person's per-(account, agent) MA vault and the credential slot for one MCP server URL. MA keeps one credential per URL: a second create is a 409, an update or delete of a vanished id is a 404. Writers: vault get-or-create, the agent-token mirror, and the OAuth grant write. | [`mcp_vault.py`](../../packages/core/daimon/core/mcp_vault.py), [`agent_mcp_credentials.py`](../../packages/core/daimon/core/agent_mcp_credentials.py), [`mcp_oauth/vault.py`](../../packages/core/daimon/core/mcp_oauth/vault.py) |
| `SlackInstall.tla` | One Slack workspace installed, uninstalled (Slack sends `app_uninstalled` and `tokens_revoked`, each a teardown) and reinstalled through the OAuth callback. A teardown may run after the reinstall began (delayed or retried delivery). | [`oauth_slack.py`](../../packages/adapters/mcp/daimon/adapters/mcp/oauth_slack.py), [`provisioning.py`](../../packages/core/daimon/core/defaults/provisioning.py), [Slack `_handle_teardown`](../../packages/adapters/slack/daimon/adapters/slack/app.py) |

## Run

```sh
set -eu
: "${TLA2TOOLS_JAR:?Set TLA2TOOLS_JAR to the path of tla2tools.jar}"
cd formal/oauth
run() { java -cp "$TLA2TOOLS_JAR" tlc2.TLC -workers 1 -metadir "${TMPDIR:-/tmp}/daimon-oauth-tlc/$2" -config "$2.cfg" "$1.tla"; }
# clean configs exit 0
for c in McpOAuthFlow; do run McpOAuthFlow $c; done
for c in VaultLocked MirrorGrantSkip MirrorRotate GrantRetry GrantLocked; do run VaultSlot $c; done
for c in DiscordRejoinShape SlackReinstallFixed; do run SlackInstall $c; done
# counterexample configs exit 12 with a trace
for c in McpOAuthFlowPreClientCAS McpOAuthFlowPreCompletion McpOAuthFlowNoConsumeCAS; do run McpOAuthFlow $c || test $? -eq 12; done
for c in VaultPreLock MirrorPreGrantSkip MirrorPreRotate GrantVsMirror GrantVsStaleMirror GrantRetryThreeMirrors; do run VaultSlot $c || test $? -eq 12; done
for c in SlackReinstallPre231 SlackLateTeardown SlackGuardWrongOrder; do run SlackInstall $c || test $? -eq 12; done
```

## Properties

| Model | Invariant | Meaning |
| --- | --- | --- |
| McpOAuthFlow | `AtMostOneConsume` | a flow is spent at most once |
| McpOAuthFlow | `AtMostOneGrant` | one flow stores at most one grant |
| McpOAuthFlow | `ExchangeMatchesIssuer` | the code is exchanged as the client it was issued to |
| McpOAuthFlow | `ConnectedImpliesGrant` | nobody counts as connected without a stored grant |
| VaultSlot | `NoDuplicateVault` | one vault per (account, agent) |
| VaultSlot | `MirrorNeverFails409` | mirroring the agent's token never fails a turn on a URL the person's grant holds |
| VaultSlot | `MirrorLeavesCurrent` | a finished mirror leaves the grant or the current token at the URL |
| VaultSlot | `MirrorNever404` | a mirror update never fails because the grant write replaced the credential |
| VaultSlot | `SignInNeverLost`, `GrantSurvives` | a consumed sign-in is never lost to a concurrent static-credential writer, and its grant is not overwritten afterwards |
| SlackInstall | `ReinstallLeavesLiveTenant` | once the reinstall and every teardown have finished, the tenant is live and holds the reinstall's token |

The grant landing in the requesting account's vault holds by construction: the
flow row carries `account_id` and the callback writes only to that account's
vault. The model does not re-check it beyond `AtMostOneGrant`.

## Calibration

Each model reproduces real past fixes as counterexamples in the pre-fix shape and
is clean with the fix, before any new counterexample was taken seriously.

| Fix | Pre-fix config | Verdict | Fixed config | Verdict |
| --- | --- | --- | --- | --- |
| `7a5a74b` a reopened sign-in link reuses the client the first open registered | `McpOAuthFlowPreClientCAS` | violates `ExchangeMatchesIssuer` | `McpOAuthFlow` | clean |
| `41234e9` a declined sign-in is not a connection | `McpOAuthFlowPreCompletion` | violates `ConnectedImpliesGrant` | `McpOAuthFlow` | clean |
| `2cbe69e` serialize vault get-or-create | `VaultPreLock` | violates `NoDuplicateVault` | `VaultLocked` | clean |
| `39450e8` mirror leaves a URL held by the caller's own grant alone | `MirrorPreGrantSkip` | violates `MirrorNeverFails409` | `MirrorGrantSkip` | clean |
| `dc2d866` propagate a rotated MCP token in place | `MirrorPreRotate` | violates `MirrorLeavesCurrent` | `MirrorRotate` | clean |
| Discord rejoin clears the archive (`clear_archive=True`, #132, before the public history; weak, see below) | `SlackReinstallPre231` is that pre-fix shape | violates `ReinstallLeavesLiveTenant` | `DiscordRejoinShape` | clean |

`McpOAuthFlowNoConsumeCAS` is a mutation check rather than a past fix: without
`used_at IS NULL` in `consume_flow`, a replayed callback spends the flow twice.

The Slack install calibration is weaker than the others: #132 predates the
public git history, so the pre-fix shape is the Discord adapter's documented
behaviour, not a recoverable commit.

## Regression rows (fixed on main)

These bugs were found while building the models and are fixed on main by
pymc-labs/daimon#230 and #231. The pre-fix configs stay as regression rows: they
must keep finding the counterexample, and the fixed configs must stay clean.
They are not independent history, so they are kept out of the calibration table
above.

- **Sign-in lost to a concurrent mirror** (`GrantVsMirror`, violates
  `SignInNeverLost`; fixed on main by #230). Before #230, `put_mcp_oauth_credential` lists the vault, deletes the
  credential at the URL and creates the grant, with no lock. A turn for the same
  person and agent can mirror the agent's shared token for that URL inside that
  window: it sees the slot empty and creates the static credential. The grant's
  create then fails with 409, after the flow and the provider's code are both
  spent, and the person sees "Sign-in did not complete". Trace: OAuth lists →
  OAuth deletes → mirror lists (empty) → mirror creates → OAuth create 409.
  Checked against the code: the callback's `except` covers
  `anthropic.AnthropicError`, so the 409 lands on the `exchange_failed` page.
- **Turn failed by the grant replacing a stale token** (`GrantVsStaleMirror`,
  violates `MirrorNever404`). The mirror lists a stale static credential, the
  grant write deletes it, and the mirror's in-place update 404s, which
  `mirror_credentials_into_vault` propagates and fails the turn.
  Both are fixed on main by pymc-labs/daimon#230, which retries against a
  fresh read (`GrantRetry`: the grant write tolerates a 404 on delete and
  re-reads on a 409, three attempts in total, `MaxTries = 2` retries; the
  mirror re-reads on a 404). `GrantRetry` is clean with two concurrent mirrors
  only; see the next section for three.

## Pre-#239 counterexample

- **Three concurrent mirrors exhausted #230's retry** (`GrantRetryThreeMirrors`,
  violates `SignInNeverLost`; fixed in pymc-labs/daimon#239). Before that fix, each
  retry of the grant write can be undone by one more turn mirroring the same
  (account, agent) vault. Trace (16 states): the grant write lists and deletes
  the static credential → mirror 1 lists the empty slot and creates the static
  credential → the grant's create 409s (retry 1) → it lists and deletes again →
  mirror 2 does the same → 409 (retry 2) → mirror 3 → the third 409 exhausts the
  attempts and the consumed sign-in is lost. With `MaxTries = 3` the same three
  mirrors are clean, so the result depends on the retry budget.
  `GrantLocked` (#239's shape: the grant write and the mirror hold the vault's
  advisory lock, and #230's retries stay for writers outside the lock) is clean
  with three mirrors.
- **Reinstall left the tenant archived** (`SlackReinstallPre231`; fixed on main
  by #231). Before #231 the Slack callback's `provision_tenant` was
  `ON CONFLICT DO NOTHING`, so a reinstall never cleared `archived_at`; hub login and the boot defaults sweep then treat the
  workspace as gone while chat keeps working.
- **Late teardown deletes the reinstall's token** (`SlackLateTeardown`): with the
  archive cleared, a teardown that runs after the reinstall still archives the
  tenant and deletes the fresh token (archive and delete are separate, unguarded
  transactions). Guarding the teardown on the event time is not enough alone:
  `SlackGuardWrongOrder` shows a teardown between the reinstall's archive clear
  and its token write re-archiving the tenant, so the reinstall must store its
  token before clearing the archive. `SlackReinstallFixed` (clear after upsert,
  guarded single-transaction teardown) is clean and matches main. Fixed on main
  by pymc-labs/daimon#231.

## Bounds and assumptions

- McpOAuthFlow: two opens, two clients, each open's callback delivered at most
  twice. Expiry is omitted (it only disables actions). The start link is assumed
  to reach only the requester; a leaked link lets another person's provider
  identity land in the requester's vault, which is a property of bearer links,
  not of these state machines.
- VaultSlot: one vault slot (one URL), two concurrent mirrors (three in
  `GrantRetryThreeMirrors` and `GrantLocked`), one grant
  write, and (for the vault configs) two concurrent session creates. Each MA
  call is atomic; list results are snapshots. `add_external_mcp_credential`
  (holds the lock), the Copilot writer and the operator credential sweep write
  other URLs or run under the lock and are not modelled; a paste racing a grant
  write on the same URL has the same shape as the mirror race.
- SlackInstall: one uninstall producing two teardowns, one reinstall; each
  transaction is atomic. The model's guard compares install generations; the
  code compares the stored token's `updated_at` with the event's `event_time`
  under the tenant row lock, which assumes Slack's and the database's clocks
  agree to within the time a person takes to reinstall.
- A passing TLC run checks only these finite abstractions.
