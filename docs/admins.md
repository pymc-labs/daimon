# Admins and who may start a turn

Who counts as a Daimon admin, what a granted admin may do, and how roles limit
who can start a turn or use an agent. [Permissions](permissions.md) covers the
channel and agent rules these people set.

## Who is an admin

Each server or workspace has one admin role, shared by all its agents and
channels. Platform admins always hold it, checked live:

| Platform | Platform admins |
| --- | --- |
| Discord | the owner, and members with Administrator or Manage Server |
| Slack | workspace admins and owners |
| Teams | owners of the [admin team](teams.md) |

Their platform authority cannot be revoked inside Daimon. A platform admin can
also grant the Daimon admin role to a user, or on Discord to a role. A role
grant holds while the member holds that role.

## What a granted admin may do

A granted admin may administer only these actions:

| Action | Covers |
| --- | --- |
| `agent_settings` | agent instructions, identity, backend and settings |
| `channel_permissions` | channel and agent rules, participation settings |
| `routines` | routine configuration |
| `skills` | agent and channel skills |
| `environments` | agent and channel environments |
| `model` | model selection |
| `routing` | who answers where, channel defaults, handoffs |
| `tool_rules` | agent and channel tool rules and their approval panels |
| `agent_lifecycle` | copying and archiving agents |
| `hub_pin_exemption` | the hub's agent-pin exemption when starting a turn |

Everything else stays with platform admins. That includes credentials, MCP
connections, coding-tool access, turning DMs on and off, GitHub, provider
allowlists, long-job policy and every money control. A granted admin gets no
hub access to other people's sessions or sealed transcripts.

Granted admins change agents that platform admins use too. Their edits to
instructions, skills, environments and tool rules also reach platform admins'
chats and routines on those agents. Owning an agent is separate: a grant never
makes someone an owner.

## Grant and revoke

Open **Who answers where**, then **Permissions**, then **Admins** on Discord,
or **Who answers where**, then **Admins** on Slack. Teams uses the CLI or MCP.
Each change rechecks platform authority and records who changed which grant
and when.

```bash
export DAIMON_ADMINS_URL=https://your-domain.example/mcp
export DAIMON_ADMINS_TOKEN=<your personal CLI credential>
daimon admins list
daimon admins grant USER_ID
daimon admins revoke ROLE_ID --role
```

The CLI calls the MCP tools `list_admins`, `grant_admin` and `revoke_admin`.
Members may list. Granting and revoking need a platform admin acting with
their own registered CLI credential. A revocation takes effect on the next
click or request, including open panels and existing credentials. Erasing a
person removes their grants.

## Changes that need a person

Changing admins and every money control need a human acting directly: a
platform panel that rechecks authority live, or a personal CLI credential
re-checked as a platform admin. Money controls are budgets, caps, billing
reads, promo redemption and checkout.

Chat turns (even an admin's own), routines, continuations, watches, agent keys
and operator tokens are refused for these. Issuing promo codes uses the local
`daimon promo` CLI only. See [billing](billing.md#promo-codes).

## Who may start a turn

A server's access policy can limit who may start turns at all:

- `invoker_user_ids` lists people who may.
- `invoker_role_ids` (Discord only) lists roles whose current holders may.
- `invoker_admin_bypass`, on by default, lets admins through either list.

The two lists form a union, and both empty means anyone. Writer and agent
rules still apply on top. Role matching uses ids, so renaming a role changes
nothing.

```bash
daimon tenants access-policy set discord GUILD_ID --invoker-role ROLE_ID --invoker-role OTHER_ID
daimon tenants access-policy set discord GUILD_ID --clear-invoker-roles
daimon tenants access-policy set discord GUILD_ID --no-invoker-admin-bypass
daimon tenants access-policy get discord GUILD_ID --json
```

`--invoker-role` replaces the role list. The server's own id (`@everyone`) is
refused, and Slack and Teams refuse any role list. With the bypass off, admins
on every platform must match the lists like anyone else.

## Limit an agent to roles

An operator can keep one Discord agent to people who currently hold one of
some roles. They must also pass the server's invoker policy. Admins need a
matching role too: the admin bypass does not cover an agent's roles.

```bash
daimon agents rule invoker-roles discord GUILD_ID AGENT_NAME --role ROLE_ID --role OTHER_ID
daimon agents rule invoker-roles discord GUILD_ID AGENT_NAME --clear
daimon tenants access-policy rules discord GUILD_ID
```

Roles are stored as `agent_rules.<agent>.invoker_role_ids`, independent of
`runs_in`. A rule on any of the agent's names applies, and archiving the agent
drops it.

Membership is checked live when the agent is chosen, on queued sends and
before each model call and approval card. A revoked role refuses the next
step, not one already running. A handoff checks the person asking against the
agent it moves to.

Only a person can use a role-gated agent. Routines, watches, timers, long jobs
and other unattended turns cannot, and saving a routine for one is refused. A
handoff from an unattended turn cannot switch to one. Handing a thread away
from a role-gated agent carries the transcript but skips the workspace
checkpoint.

## How membership is checked

Discord checks use `DAIMON_DISCORD__BOT_TOKEN`, so set it on the scheduler and
MCP processes as well as the Discord service. Ordinary checks cache a positive
answer for 30 seconds and a negative one for five. Private delivery always
looks up live. A failed lookup never grants access.
