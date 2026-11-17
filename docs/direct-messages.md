# Direct messages

How people talk to Daimon privately, and what an operator turns on for it.
Read [isolation](isolation.md#direct-messages) for what a DM can see and
[Slack](slack.md#use-private-conversations) for Slack's own setup.

There are three ways into a DM:

| Way in | Platforms | Starts with |
| --- | --- | --- |
| `/dm-me` in a channel | Discord, Slack | that channel's recent text and a link back |
| A cold DM to the bot | Discord, Slack, Teams | an empty conversation with the agent chosen below |
| A routine or watch that reports by DM | Discord | a fresh private turn per run |

All three need the server's DMs turned on (`/dm-me enable`, or `daimon features
set direct_messages on`) and a recipient policy that allows the person.

## Turn on cold DMs

A cold DM is a first message sent straight to the bot, with no `/dm-me`. It needs
the server's DMs on and an agent that has DMs on and admits the person. The
default agent below is optional: without one, a sole admitting agent answers and
several show a picker.

```bash
daimon features --guild GUILD_ID set direct_messages on
daimon dm --guild GUILD_ID default AGENT_NAME     # optional: who answers by default
daimon dm --guild GUILD_ID default                # show it; --clear unsets it
```

An admin can do the same in Discord with `/dm-me action:enable`, then
`/dm-me action:agent agent_name:AGENT_NAME` (`clear-agent` unsets it). The
default agent is one value for cold DMs, `/dm-me`, the picker and agent-sent
notices. The CLI, Discord, the web app and MCP all set it through one writer
that checks the name and audits the change. Clearing it does not stop cold DMs:
turn DMs off, or limit who each agent admits, for that.

A Slack or Teams DM belongs to one workspace, so a cold DM there starts at
once with the agent chosen below. Teams personal chats have DMs on unless an
admin turns them off.

Upgrade every Discord process before turning cold DMs on, since older ones do
not recognise a conversation with no source channel. The bot token must be set
on the scheduler and MCP processes too, for the membership checks below.

## Choose who may DM which agent

Each agent has a DM grant, and each server has a default agent, ordered rules
and, per person, an optional pick. A person's private conversation starts
with the first agent that has DMs on and admits them, chosen like this:

1. **Grants.** An agent admits `members` (the default: every member), only
   those `listed`, or everyone `excluded` (all but those listed). A grant
   lists people and groups: Discord roles, Slack user groups, or Teams teams
   the app is installed in. If a group lookup fails, the agent is treated as
   closed for that person.
2. **Rules.** The first rule that matches the person (by user, group or
   everyone) names an agent. A **locked** rule decides alone: the person's pick
   is ignored, and if its agent is unavailable to them they are refused.
3. **Pick, rule agent, default.** Otherwise the first available of the person's
   pick, the rule's agent and the server default. When several agents remain, a
   picker (a dropdown, or buttons on Teams) lets the person choose, and the choice is
   remembered for the next conversation. Messages sent before the choice wait for
   it, up to five minutes, and are answered in one turn. A refused choice ends the
   wait at once.

`/dm-me` in a channel follows the same rules and refuses a locked-out person
with a private note. Handing a DM to another agent in the conversation is
held to the same grant. Agents that send a DM by themselves (the
`send_direct_message` tool, watches, routines and budget notices) are held to
the sending agent's grant only; locks do not apply.

```bash
daimon features --guild GUILD_ID set direct_messages on   # or off; `default` clears it
daimon dm --guild GUILD_ID default AGENT                  # the default agent
daimon dm --guild GUILD_ID access AGENT --mode listed --user 123 --group 456
daimon dm --guild GUILD_ID rule add --group 456 --agent AGENT --locked
daimon dm --guild GUILD_ID rule list | move | remove
daimon dm --guild GUILD_ID explain 123   # who would answer, and why (groups read live)
daimon dm import-policies --dry-run      # DAIMON_DIRECT_MESSAGE_POLICIES as stored access
```

`--guild GUILD_ID` (or `--tenant TENANT_UUID`) picks the server and goes after
`dm` or `features`, before the command; without it the CLI's own tenant is used. As
in the web app, an unknown agent name is refused and each change is in the
audit log, under the CLI's local user. Erasing a person removes their pick,
their personal rules and their entry in any listed grant.

`DAIMON_DIRECT_MESSAGE_POLICIES` is deprecated. While it is set it applies with
the stored access on every path, `dm explain` and "Check a person" included, so
the stricter wins; the Discord service logs a warning. `daimon dm
import-policies` stores it: an allowlist becomes a listed grant on every agent
and `disabled` turns DMs off. The setting can then be removed. A notice no agent
sends, such as a budget notice, is held to the default agent's grant, so set a
default agent before removing an allowlist.

## What a person sees

When someone DMs the bot, Daimon looks for servers where they are a current
member, pass the invoker policy and have an agent that admits them.

- **One server** starts a conversation and answers the message.
- **Several servers** show a server picker only that person can use. It holds
  their messages for five minutes and checks access again on the pick.
- **None** runs no turn. A member refused by the invoker policy, or locked out
  by a locked rule, is told they may not DM Daimon. Anyone else (including a
  person no agent's grant, role requirement or DM switch admits) gets the
  `/dm-me` guidance, which names no server.

The opening message names the server and agent, and offers **Switch server**
and **New conversation**. `/dm-settings` shows those buttons again, and `/new`
starts an empty conversation with the same server and the agent routing picks.
Buttons expire after fifteen minutes, and only the latest set works.

A cold conversation starts with no history and no channel access. Switching
server or starting a new one never carries the old history across. An active
conversation always wins over discovery, so a message never moves to another
server, even after access is revoked.

## Inside a Discord DM

- **Progress card.** A DM turn shows the same card as a thread, with Stop,
  and the answer lands in the card. A restart leaves the card at its last
  update.
- **Approvals.** A write that needs approval posts its card in the DM, and
  only the sender can answer. Slack and Teams DMs have no card, so such a write
  is refused there.
- **Files.** Files the agent saves are attached to the answer, under Discord's
  10 MB DM limit. A larger file stays in the session with a notice.
- **Cost line.** The answer ends with the usual summary line unless an admin
  hid it under Cost footer in `/settings`.

Before every card update, output chunk and file, Daimon checks the person may
still receive the answer. That check applies the server's DM switch, the
recipient policy and the agent's rules, with no admin exemption. The first
refusal ends delivery silently for that turn.

## Membership checks

Discord access is checked against current server membership and role ids,
using `DAIMON_DISCORD__BOT_TOKEN`. The bot must be in the server and able to
read members, roles and server details.

A positive answer is cached for at most 30 seconds and a negative one for
five. Private delivery always does a live lookup. A failed or slow lookup
never grants access; discovery asks the person to try again instead.

Slack user groups need the `usergroups:read` scope, so reinstall the app
after adding it. Teams teams are read from the team's roster. A group lookup
is fresh when a conversation starts and cached for 60 seconds after.

The stock Discord service does not request the privileged Server Members
intent, so it relies on that short cache. A gateway that turns the intent on
gets role changes and departures at once.

## Limits

- Discovery and server switching share three attempts per person, refilling
  one every 20 seconds. Running out is silent.
- A picker carries its own expiry: a late pick is refused, and the person sends
  a new message. A process that stops while a pick is open says the held
  messages were not sent, except on Teams, which takes them up after the restart.
- Discord DMs reach one process. On Slack and Teams, a pick made on another
  process is found within seconds; if two processes each offer a picker, the
  first pick wins. A pick refused on another process ends the wait only when the
  picker expires.
- Routine and watch reports never pick a server or join the active
  conversation's history.
- Turning DMs off stops new turns and, on Discord, the delivery of answers
  still running.
