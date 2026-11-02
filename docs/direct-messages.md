# Direct messages

How people talk to Daimon privately, and what an operator turns on for it.
Read [isolation](isolation.md#direct-messages) for what a DM can see and
[Slack](slack.md#use-private-conversations) for Slack's own setup.

There are three ways into a DM:

| Way in | Platforms | Starts with |
| --- | --- | --- |
| `/dm-me` in a channel | Discord, Slack | that channel's recent text and a link back |
| A cold DM to the bot | Discord | an empty conversation with the server's DM agent |
| A routine or watch that reports by DM | Discord | a fresh private turn per run |

All three need the server's DMs turned on (`/dm-me enable`) and a recipient
policy that allows the person.

## Turn on cold DMs

A cold DM is a first message sent straight to the bot, with no `/dm-me`.
Cold DMs stay off until a server names its DM agent. The agent must already
exist in that server; Daimon never falls back to the default agent.

```bash
daimon tenants dm-policy discord GUILD_ID --agent AGENT_NAME --enabled
daimon tenants dm-policy discord GUILD_ID --json
daimon tenants dm-policy discord GUILD_ID --clear-agent
```

An admin can do the same in Discord with `/dm-me action:enable`, then
`/dm-me action:agent agent_name:AGENT_NAME`. Clearing the agent stops cold DMs
without closing existing conversations or `/dm-me` moves.

Upgrade every Discord process before naming an agent, since older ones do not
recognise a conversation with no source channel. The bot token must be set on
the scheduler and MCP processes too, for the membership checks below.

## What a person sees

When someone DMs the bot, Daimon looks for servers where they are a current
member, pass the invoker policy and may use the DM agent.

- **One server** starts a conversation and answers the message.
- **Several servers** show a server picker only that person can use. It keeps
  the message for five minutes and checks access again on the pick.
- **None** runs no turn. A member refused by the invoker policy or the agent's
  role requirement is told they may not DM Daimon. Anyone else gets the
  `/dm-me` guidance, which names no server.

The opening message names the server and agent, and offers **Switch server**
and **New conversation**. `/dm-settings` shows those buttons again, and `/new`
starts an empty conversation with the same server and its current DM agent.
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
  hid it with `/cost-footer hide`.

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

The stock Discord service does not request the privileged Server Members
intent, so it relies on that short cache. A gateway that turns the intent on
gets role changes and departures at once.

## Limits

- Discovery and server switching share three attempts per person, refilling
  one every 20 seconds. Running out is silent.
- Pickers and buttons live in one process. A restart drops them, and the
  person sends a new message.
- Routine and watch reports never pick a server or join the active
  conversation's history.
- Turning DMs off stops new turns and, on Discord, the delivery of answers
  still running.
