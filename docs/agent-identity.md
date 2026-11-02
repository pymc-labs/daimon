# Agent identity

How Daimon shows each agent's own name and avatar on its messages, what each
platform allows, and what an operator must set up first. Read it before you
turn agent identity on.

By default every agent speaks as the one Daimon bot. With agent identity on,
each message shows its agent's name and avatar in the platform's own message
header, on every chunk of a multi-message answer. A reply to an agent's message
can reach that agent.

## What users see

| Platform | What changes |
| --- | --- |
| Slack | each message posts with the agent's name and avatar; the footer omits the name |
| Discord | each message posts through a channel webhook with the agent's name and avatar |
| Teams | a bold name prefix on the first chunk when the agent is not the built-in one |

The built-in Daimon agent, used for setup conversations and as the deployment
default, always keeps the app's own name and icon.

With identity off, agents post as the bot. Slack keeps the agent name in the
footer, Discord uses ordinary bot posts and needs a mention to start a turn,
Teams adds no prefix, and setup panels hide avatar controls.

## What each platform allows

| Platform | Mechanism | Granularity | Limits |
| --- | --- | --- | --- |
| Slack | `username` and `icon_url` on each post, needing the `chat:write.customize` scope | per message | files a turn uploads still show the bot; ephemeral panel and error replies are unchanged |
| Discord | a channel webhook with a name and avatar per message | per message | needs **Manage Webhooks**; no webhooks in DMs; one webhook per channel |
| Teams | the bot's registered identity | per bot registration | no per-message name or avatar |

Daimon does not use embeds, separate apps or bots per agent, or Teams Adaptive
Card headers.

## Turn it on

Set `DAIMON_AGENT_IDENTITY__ENABLED` to `true`, after the platform changes
below, and restart the deployment's services
([configuration.md](configuration.md#daimon_agent_identity__enabled)).

To keep a server or workspace on the bot identity, list it in
`DAIMON_AGENT_IDENTITY__EXCLUDED_DISCORD_GUILD_IDS` or
`DAIMON_AGENT_IDENTITY__EXCLUDED_SLACK_TEAM_IDS`
([Discord](configuration.md#daimon_agent_identity__excluded_discord_guild_ids),
[Slack](configuration.md#daimon_agent_identity__excluded_slack_team_ids)).
Each is a JSON array of IDs. An excluded workspace behaves as if identity were
off throughout. Slack and Discord DMs follow the global switch.

### Slack

Add the `chat:write.customize` scope to the app and reinstall it in each
workspace ([slack.md](slack.md)). Without the scope, Slack rejects the call.
Daimon then retries without the name and avatar and remembers that for the
installation's token, so an install that has not re-consented falls back to the
bot look instead of failing. A reinstall clears it.

### Discord

The bot needs **Manage Webhooks** in every channel it answers in. Add it to the
invite permissions and grant it to the bot role on existing servers
([quickstart.md](quickstart.md), [self-hosting.md](self-hosting.md)).

Daimon falls back to a bot post, with the agent's name as a small line above the
first chunk of each answer, in DMs, in voice and stage text chats, in locked
threads, and wherever Manage Webhooks is missing. After a refusal a channel
backs off webhook creation for 60 seconds.

With identity on and the permission missing, `/agent-setup` shows admins a line
saying new agent posts may show as Daimon, with a re-authorize link. When the
server grants it, the panel checks up to 10 channels and names any channel
whose overwrites deny it, and which role or entry to change. Members do not see
this.

A webhook Daimon already holds keeps posting. If the webhook is deleted and
recreated, messages from the old one can no longer be edited, and an edit that
fails that way posts the update as a new message.

### Teams

No setup is needed. Teams gets the name prefix only.

## Results Daimon posts for an agent

A routine result the agent did not post itself goes out under the agent's
identity on Slack and Discord. The answer to a submitted Discord form is posted
the same way as a mention turn. On Discord the agent's name is dropped from the
result's first line, since the header carries it. Slack keeps it, because Slack
drops the header silently without the scope.

## Avatars

Each agent gets a generated face when it is created, whichever way it is made:
a setup panel, `create_agent`, `daimon agents create` or a copy. An agent made
before faces existed, or whose render failed, gets one on its first turn with
identity on. Faces are 512 by 512 mascot variants chosen to differ from the
agent faces already in the tenant. The built-in agent keeps its fixed platform
avatar.

An admin can reset a face from a **Picture** row on the agent's detail screen
with **Use default**, recorded in the panel audit. Custom uploads are turned
off, and an agent that already has an uploaded picture keeps it until an admin
resets it. Renaming an agent keeps its face. Archiving or deleting the agent
deletes it.

> **Warning:** Avatars are public. The MCP service serves each one at an
> unguessable URL with no tenant or agent name in it, but anyone who sees a
> message can open the image. Platform and browser caches may keep it after
> Daimon deletes it.

## Reply routing

With identity on, a reply to an agent's message goes to the agent that wrote
it, where the platform says which message was answered.

1. On Discord, the message a reply points at.
2. On Discord and Slack, the thread's root post.

The agent must be one Daimon recorded as the author. Notices, setup turns,
files, posts from other bots and agents that were archived give no candidate,
and `daimon audit prune` removes old records, so an old reply routes as it
would without identity.

The reply goes through the same admission as any turn: writers, `runs_in`, the
agent's home and budgets. This routing is implicit, so it never overrides
anything explicit. When an explicit choice would be refused, such as a thread
bound to another agent or an agent that is unavailable or hidden here, Daimon
drops the candidate silently and routes as usual. A bare channel message still
goes to the default agent, and the answer shows who answered.

Slack starts turns only on a mention or a DM, so Slack routes a thread by its
root and binding, never by the chunk replied to. Unmentioned thread replies
stay unanswered.

On Discord a message also counts as addressed to Daimon when it is a reply to a
recorded agent post, without a mention.
