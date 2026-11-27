# Approvals

An approval is a card that asks the requester to say yes before an agent does
something that changes the outside world. This page covers when Daimon asks, who can
answer and where an admin sets it. [Permissions](permissions.md) covers the rules
for who may read and write where.

## When Daimon asks

In an attended chat turn, an agent that calls a write tool on a connected service
posts an **Approve** card first. The card names the action and who may answer, with
the details behind a button. Only the requester can press Approve or Deny, and the
card expires after ten minutes. Stopping the turn cancels it, so a late Approve runs
nothing.

Some actions always ask, whatever the setting: Daimon's own confirmed actions,
publishing, and permission changes. Writes are asked about only while
[`DAIMON_TOOL_SAFETY__ENABLED`](configuration.md#daimon_tool_safety__enabled) is on. A
read never asks.

## Ask or allow writes

The **Approvals** feature has two values. **Ask** is the default and posts the card
for a third-party write. **Allow writes** lets the write run without a card, so use it
only for agents and places you trust.

It is set at the workspace, agent, channel and thread levels, and the most specific
choice wins. Only a chat admin or the operator can change it, and each change
rechecks their standing and is audited. Allow writes covers chat replies only.
Routines and watches follow the [unattended rule](unattended.md#how-tool-calls-are-decided).

## Approvals per agent

An agent's own Approvals sits between the workspace and the channel. One agent can
Allow writes while the rest Ask, and a channel or thread setting still wins. An
agent that calls other agents decides their writes with its own value. See
[Helpers and handoffs](helpers.md).

```bash
daimon features set approvals off --tier agent --agent NAME --reason "trusted agent"
```

A deleted agent's value is cleared with `--agent ID`. Admins may also set it in
`/settings`, or by asking an agent behind a Confirm card.

<!-- knowledge: web -->
The web app sets it too.
<!-- /knowledge -->

## Approvals in direct messages

A direct message has no channel or thread, so it ignores Approvals and asks before
every write. Turn on **Approvals in direct messages** (`dm_approvals`) at the
workspace level, and a DM follows its agent's Approvals value, or the workspace's
when the agent has none.

```bash
daimon features set dm_approvals on --tier workspace --reason "DMs follow approvals"
```

On Discord the card appears in the DM, and only the sender can answer. Slack and Teams
DMs have no card, so a write that needs approval is refused there. Only a native
platform admin or the operator can change this setting. See
[Direct messages](direct-messages.md).

## Other approvals

- **Watches** ask the owner to approve creating, resuming and editing, unless an
  admin sets `watch_approval` to skip. See [Watches](watches.md#manage-watches).
- **Long jobs** show the command and limits on the card. See [Long jobs](long-jobs.md).
- **Always allow.** A current admin may press it on a write card to save an
  agent-wide allow. See [Permissions](permissions.md#agent-tool-rules).
