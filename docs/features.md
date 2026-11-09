# Features

Every feature is set at one or more levels: deployment, server, agent, channel and
thread. Each level is On, Off or Default, or one of the feature's values. Default
inherits from the level above. Settings show which level decided.

An admin can lock a value at the deployment, server, agent or channel level. Levels
below then cannot change it. `disabled` means Off and locked.

Set the deployment level with `DAIMON_FEATURES__<KEY>`, for example
`DAIMON_FEATURES__PROGRESS_PLANS=on:locked`; a dot in a key is an
underscore (`DAIMON_FEATURES__TOOL_BASH`). Set the others with `/settings` in chat,
the `change_setting` MCP tool, the web app or `daimon features`.

## Approvals

Ask before an agent writes to a third-party service. Decided most specific first: only a native platform admin, checked live, may change it, and it applies only once the operator enables approval overrides.

| | |
| --- | --- |
| Key | `approvals` |
| Values | `on` (Ask), `off` (Allow writes) |
| Default | `on` |
| Levels | Server, Channel, Thread |
| Combines | Most specific wins |

## Thread participation

Reply in a thread without being mentioned when a short classifier call says the agent can help. A lower tier may turn it on under a higher Off, as before the registry; a Lock stops that.

| | |
| --- | --- |
| Key | `participation` |
| Values | `on` (On), `off` (Off) |
| Default | `off` |
| Levels | Deployment, Server, Agent, Channel, Thread |
| Combines | Most specific wins |
| Deprecated | `DAIMON_THREAD_PARTICIPATION__MODE` |

## Progress plans

The agent keeps a short plan of a longer task's steps on the working card, with one-line updates on what it found and does next.

| | |
| --- | --- |
| Key | `progress_plans` |
| Values | `on` (On), `off` (Off) |
| Default | `off` |
| Levels | Deployment, Server, Agent, Channel, Thread |
| Combines | Most specific wins |
| Deprecated | `DAIMON_PROGRESS_PLANS__MODE` |

## Cost footer

Finished answers end with a footer naming the agent, the time taken, what the answer cost and the money left.

| | |
| --- | --- |
| Key | `cost_footer` |
| Values | `shown` (Shown), `hidden` (Hidden) |
| Default | `shown` |
| Levels | Server, Channel, Thread |
| Combines | Most specific wins |

## Repository memory

Each message starts with the notes the agent keeps in its repository memory. The operator turns it on, with the memory tools; below that it can only be turned off, which also switches the memory tools off.

| | |
| --- | --- |
| Key | `repo_memory` |
| Values | `on` (On), `off` (Off) |
| Default | `off` |
| Levels | Deployment, Server, Agent, Channel, Thread |
| Combines | Off above is a ceiling |
| Deprecated | `DAIMON_REPO_MEMORY__ENABLED` |

## Context provider

A context provider set up for this channel or agent adds what it knows to each message. Off skips it here. Managed Agents only.

| | |
| --- | --- |
| Key | `context_provider` |
| Values | `on` (On), `off` (Off) |
| Default | `on` |
| Levels | Deployment, Server, Agent, Channel, Thread |
| Combines | Off above is a ceiling |
| Levels on Gemini | Not available |
| Levels on OpenAI | Not available |

## Chat history

How many earlier chat messages a new conversation reads, up to 100 from the thread and 25 from the channel around it. The smallest setting wins.

| | |
| --- | --- |
| Key | `history_depth` |
| Values | `100` (100 messages), `50` (50 messages), `25` (25 messages), `10` (10 messages), `0` (None) |
| Default | `100` |
| Levels | Deployment, Server, Agent, Channel |
| Combines | Lowest wins |

## Setup guidance

Instructions at the top of the agent's prompt on where its keys, repo, memory and files are and what it may share. Off leaves the agent's own prompt and the safety rules. Applies when the agent is next saved or synced.

| | |
| --- | --- |
| Key | `guidance` |
| Values | `on` (On), `off` (Off) |
| Default | `on` |
| Levels | Deployment, Server, Agent |
| Combines | Most specific wins |

## Daimon tools

Daimon's own tools: chat, settings, routines, memory and the rest. On keeps at least the conversation ids the tools need. An isolated agent never has them. Gemini and OpenAI agents always have them, so it can't be changed there. Applies from the next message in a conversation.

| | |
| --- | --- |
| Key | `daimon_tools` |
| Values | `on` (On), `off` (Off) |
| Default | `on` |
| Levels | Deployment, Server, Agent, Channel, Thread |
| Combines | Most specific wins |

## Built-in tool: bash

The agent's built-in bash tool. The agent level is the agent's own tool settings. On never widens what a self-hosted harness allows. Applies from the next message in a conversation.

| | |
| --- | --- |
| Key | `tool.bash` |
| Values | `on` (On), `off` (Off) |
| Default | `on` |
| Levels | Deployment, Server, Agent, Channel, Thread |
| Combines | Most specific wins |
| Levels on Gemini | Deployment, Server, Agent |
| Levels on OpenAI | Deployment, Server, Agent |

## Built-in tool: read

The agent's built-in read tool. The agent level is the agent's own tool settings. On never widens what a self-hosted harness allows. Applies from the next message in a conversation.

| | |
| --- | --- |
| Key | `tool.read` |
| Values | `on` (On), `off` (Off) |
| Default | `on` |
| Levels | Deployment, Server, Agent, Channel, Thread |
| Combines | Most specific wins |
| Levels on Gemini | Deployment, Server, Agent |
| Levels on OpenAI | Deployment, Server, Agent |

## Built-in tool: edit

The agent's built-in edit tool. The agent level is the agent's own tool settings. On never widens what a self-hosted harness allows. Applies from the next message in a conversation.

| | |
| --- | --- |
| Key | `tool.edit` |
| Values | `on` (On), `off` (Off) |
| Default | `on` |
| Levels | Deployment, Server, Agent, Channel, Thread |
| Combines | Most specific wins |
| Levels on Gemini | Deployment, Server, Agent |
| Levels on OpenAI | Deployment, Server, Agent |

## Built-in tool: write

The agent's built-in write tool. The agent level is the agent's own tool settings. On never widens what a self-hosted harness allows. Applies from the next message in a conversation.

| | |
| --- | --- |
| Key | `tool.write` |
| Values | `on` (On), `off` (Off) |
| Default | `on` |
| Levels | Deployment, Server, Agent, Channel, Thread |
| Combines | Most specific wins |
| Levels on Gemini | Deployment, Server, Agent |
| Levels on OpenAI | Deployment, Server, Agent |

## Built-in tool: grep

The agent's built-in grep tool. The agent level is the agent's own tool settings. On never widens what a self-hosted harness allows. Applies from the next message in a conversation.

| | |
| --- | --- |
| Key | `tool.grep` |
| Values | `on` (On), `off` (Off) |
| Default | `on` |
| Levels | Deployment, Server, Agent, Channel, Thread |
| Combines | Most specific wins |
| Levels on Gemini | Deployment, Server, Agent |
| Levels on OpenAI | Deployment, Server, Agent |

## Built-in tool: glob

The agent's built-in glob tool. The agent level is the agent's own tool settings. On never widens what a self-hosted harness allows. Applies from the next message in a conversation.

| | |
| --- | --- |
| Key | `tool.glob` |
| Values | `on` (On), `off` (Off) |
| Default | `on` |
| Levels | Deployment, Server, Agent, Channel, Thread |
| Combines | Most specific wins |
| Levels on Gemini | Deployment, Server, Agent |
| Levels on OpenAI | Deployment, Server, Agent |

## Built-in tool: web fetch

The agent's built-in web fetch tool. The agent level is the agent's own tool settings. On never widens what a self-hosted harness allows. Applies from the next message in a conversation.

| | |
| --- | --- |
| Key | `tool.web_fetch` |
| Values | `on` (On), `off` (Off) |
| Default | `on` |
| Levels | Deployment, Server, Agent, Channel, Thread |
| Combines | Most specific wins |
| Levels on Gemini | Deployment, Server, Agent |
| Levels on OpenAI | Deployment, Server, Agent |

## Built-in tool: web search

The agent's built-in web search tool. The agent level is the agent's own tool settings. On never widens what a self-hosted harness allows. Applies from the next message in a conversation.

| | |
| --- | --- |
| Key | `tool.web_search` |
| Values | `on` (On), `off` (Off) |
| Default | `on` |
| Levels | Deployment, Server, Agent, Channel, Thread |
| Combines | Most specific wins |
| Levels on Gemini | Deployment, Server, Agent |
| Levels on OpenAI | Deployment, Server, Agent |

## Message controls

Facts sent with each message: where it was sent, who is answering, and how to use them. Origin only keeps the ids and the honesty facts; Minimal keeps only the facts about the model and the conversation. Daimon tools keep it at Origin only or more.

| | |
| --- | --- |
| Key | `turn_controls` |
| Values | `full` (Full), `origin` (Origin only), `minimal` (Minimal) |
| Default | `full` |
| Levels | Deployment, Server, Agent |
| Combines | Most specific wins |

## Web app links

Each message tells the agent the web app's address and the pages it may link to.

| | |
| --- | --- |
| Key | `web_awareness` |
| Values | `on` (On), `off` (Off) |
| Default | `on` |
| Levels | Deployment, Server, Agent, Channel, Thread |
| Combines | Most specific wins |

## Connected server briefing

Before each chat message, the agent's connected servers that offer a context tool are sent the message, and their answers go in front of it as quoted data. Managed Agents only.

| | |
| --- | --- |
| Key | `mcp_briefing` |
| Values | `on` (On), `off` (Off) |
| Default | `on` |
| Levels | Deployment, Server, Agent, Channel, Thread |
| Combines | Most specific wins |
| Levels on Gemini | Not available |
| Levels on OpenAI | Not available |
| Deprecated | `DAIMON_MCP_CONTEXT__ENABLED` |

## Run instructions

A short note in front of a message that did not come from chat: a routine, a relayed draft, a handoff or another agent's request, with any wording the agent's spec adds. Off sends the message alone.

| | |
| --- | --- |
| Key | `origin_fragments` |
| Values | `on` (On), `off` (Off) |
| Default | `on` |
| Levels | Agent |
| Combines | Most specific wins |

## Keys file

The agent's saved keys as a .env file in its working files. Off leaves the file out; the keys stay saved. Applies from the next message in a conversation. Managed Agents only.

| | |
| --- | --- |
| Key | `agent_env_file` |
| Values | `on` (On), `off` (Off) |
| Default | `on` |
| Levels | Agent |
| Combines | Most specific wins |
| Levels on Gemini | Not available |
| Levels on OpenAI | Not available |

## Carry working files

When a long conversation starts over, the agent is first asked to save its working files to carry across. Off carries only the conversation and skips that extra paid step. Managed Agents only.

| | |
| --- | --- |
| Key | `checkpoint_prompt` |
| Values | `on` (On), `off` (Off) |
| Default | `on` |
| Levels | Deployment, Server, Agent |
| Combines | Most specific wins |
| Levels on Gemini | Not available |
| Levels on OpenAI | Not available |

## Platform prompt

The agent's prompt is adjusted to the chat platform's words and formatting. Off sends the agent's own prompt as written. A conversation keeps what it started with until /new. OpenAI agents always get their own prompt.

| | |
| --- | --- |
| Key | `platform_prompt` |
| Values | `on` (On), `off` (Off) |
| Default | `on` |
| Levels | Deployment, Server, Agent |
| Combines | Most specific wins |

## Platform skills

The skills Daimon adds for the chat platform, such as its formatting. A conversation keeps what it started with until /new. Managed Agents only.

| | |
| --- | --- |
| Key | `platform_skills` |
| Values | `on` (On), `off` (Off) |
| Default | `on` |
| Levels | Deployment, Server, Agent, Channel |
| Combines | Most specific wins |
| Levels on Gemini | Not available |
| Levels on OpenAI | Not available |

## Agent skills

The skills listed on the agent. Off leaves them out of new conversations; the agent keeps its list. A conversation keeps what it started with until /new. Always on for Gemini and OpenAI agents.

| | |
| --- | --- |
| Key | `agent_skills` |
| Values | `on` (On), `off` (Off) |
| Default | `on` |
| Levels | Deployment, Server, Agent, Channel |
| Combines | Most specific wins |

## Channel skills

The skills added to a channel. They need the read tool, so they are off wherever it is. A conversation keeps what it started with until /new. Managed Agents only.

| | |
| --- | --- |
| Key | `channel_skills` |
| Values | `on` (On), `off` (Off) |
| Default | `on` |
| Levels | Server, Agent, Channel |
| Combines | Most specific wins |
| Levels on Gemini | Not available |
| Levels on OpenAI | Not available |

## Personal memory

Each person's own memory with the agent, in conversations only they can read. On never adds it anywhere else. A conversation keeps what it started with until /new. Off for Gemini and OpenAI agents.

| | |
| --- | --- |
| Key | `memory_personal` |
| Values | `on` (On), `off` (Off) |
| Default | `on` |
| Levels | Deployment, Server, Agent, Channel |
| Combines | Off above is a ceiling |

## Shared memory

The agent's memory shared by everyone who talks to it. A conversation keeps what it started with until /new. Always on for Gemini and OpenAI agents.

| | |
| --- | --- |
| Key | `memory_shared` |
| Values | `on` (On), `off` (Off) |
| Default | `on` |
| Levels | Deployment, Server, Agent, Channel |
| Combines | Off above is a ceiling |

## Channel memory

The memory a restricted channel keeps for itself in place of the shared one. A conversation keeps what it started with until /new. Managed Agents only.

| | |
| --- | --- |
| Key | `memory_channel` |
| Values | `on` (On), `off` (Off) |
| Default | `on` |
| Levels | Server, Agent, Channel |
| Combines | Off above is a ceiling |
| Levels on Gemini | Not available |
| Levels on OpenAI | Not available |

## Thread titles

A thread Daimon starts is titled from its opening message by a short model call.

| | |
| --- | --- |
| Key | `thread_naming` |
| Values | `on` (On), `off` (Off) |
| Default | `on` |
| Levels | Deployment, Server, Channel |
| Combines | Off above is a ceiling |
| Deprecated | `DAIMON_THREAD_NAMING__ENABLED` |

## Typed mentions

A message with "@" and the agent's name typed as plain text is answered like a real mention. Direct messages and bots are unchanged.

| | |
| --- | --- |
| Key | `plain_text_mentions` |
| Values | `on` (On), `off` (Off) |
| Default | `off` |
| Levels | Deployment, Server, Channel |
| Combines | Most specific wins |

## Completion pings

The person who asked is pinged when a long answer is finished.

| | |
| --- | --- |
| Key | `completion_pings` |
| Values | `on` (On), `off` (Off) |
| Default | `off` |
| Levels | Deployment, Server, Channel |
| Combines | Most specific wins |
| Deprecated | `DAIMON_COMPLETION_PINGS` |

## Table images

Tables in an answer are drawn as an image or a native table instead of text.

| | |
| --- | --- |
| Key | `table_rendering` |
| Values | `on` (On), `off` (Off) |
| Default | `on` |
| Levels | Deployment, Server, Channel |
| Combines | Most specific wins |
| Deprecated | `DAIMON_TABLE_RENDERING` |

## Budget notices

Admins are told in a direct message when a channel's budget is nearly used or spent.

| | |
| --- | --- |
| Key | `budget_notices` |
| Values | `on` (On), `off` (Off) |
| Default | `on` |
| Levels | Deployment, Server |
| Combines | Most specific wins |
| Deprecated | `DAIMON_BUDGET_NOTICES` |

## Keep messages apart

An answer made of several agent messages keeps each one, set apart by a blank line, instead of joining them into one block.

| | |
| --- | --- |
| Key | `status_log` |
| Values | `on` (On), `off` (Off) |
| Default | `off` |
| Levels | Deployment, Server, Channel |
| Combines | Most specific wins |
| Deprecated | `DAIMON_TURN__STATUS_LOG` |

## Cost on hosted-model footers

A finished answer from an OpenAI or Gemini agent shows what it cost, or that the cost is unknown or partial. Managed Agents footers are unchanged.

| | |
| --- | --- |
| Key | `native_footer_cost` |
| Values | `on` (On), `off` (Off) |
| Default | `off` |
| Levels | Deployment, Server |
| Combines | Most specific wins |
| Deprecated | `DAIMON_TURN__NATIVE_FOOTER_COST` |

## Vote buttons

Finished answers get thumbs up and down for feedback.

| | |
| --- | --- |
| Key | `message_feedback` |
| Values | `on` (On), `off` (Off) |
| Default | `on` |
| Levels | Deployment, Server, Channel |
| Combines | Most specific wins |

## Reply hint

A short reminder that a reply needs a mention, where an unmentioned message is skipped.

| | |
| --- | --- |
| Key | `unmentioned_reply_hint` |
| Values | `on` (On), `off` (Off) |
| Default | `off` |
| Levels | Deployment, Server, Channel |
| Combines | Most specific wins |
| Deprecated | `DAIMON_DISCORD__UNMENTIONED_REPLY_HINT` |

## No-writers notice

Says so when a channel's write access is set to no one and a message is skipped.

| | |
| --- | --- |
| Key | `writers_none_notice` |
| Values | `on` (On), `off` (Off) |
| Default | `off` |
| Levels | Deployment, Server, Channel |
| Combines | Most specific wins |
| Deprecated | `DAIMON_DISCORD__WRITERS_NONE_NOTICE` |

## Waiting card

A card shows in a channel whose reply is waiting on the team's limit on replies at once.

| | |
| --- | --- |
| Key | `waiting_card` |
| Values | `on` (On), `off` (Off) |
| Default | `off` |
| Levels | Deployment, Server |
| Combines | Most specific wins |
| Deprecated | `DAIMON_DISCORD__TENANT_CAP_WAITING_CARD` |

## Private channel messages

Every message in private and shared channels is read through Microsoft Graph, not only the ones that mention the agent.

| | |
| --- | --- |
| Key | `private_channel_messages` |
| Values | `on` (On), `off` (Off) |
| Default | `off` |
| Levels | Deployment, Server, Channel |
| Combines | Off above is a ceiling |
| Deprecated | `DAIMON_PRIVATE_CHANNEL_MESSAGES__ENABLED` |

## Direct messages

Conversations may move to a direct message with the agent. The agent that answers there is the team's DM agent, which is chosen apart from this.

| | |
| --- | --- |
| Key | `direct_messages` |
| Values | `on` (On), `off` (Off) |
| Default | `off` |
| Levels | Deployment, Server |
| Combines | Off above is a ceiling |

## Read-only memory in direct messages

A reply in a direct message can read the agent's memory but not change it.

| | |
| --- | --- |
| Key | `dm_memory_read_only` |
| Values | `off` (Writable), `on` (Read-only) |
| Default | `off` |
| Levels | Deployment, Server |
| Combines | Stricter wins |

## Web app: chat

The chat pages of the web app. Off hides them for everyone on the team.

| | |
| --- | --- |
| Key | `web.chat` |
| Values | `on` (On), `off` (Off) |
| Default | `on` |
| Levels | Deployment, Server |
| Combines | Off above is a ceiling |

## Web app: agents

The agents pages of the web app. Off hides them for everyone on the team.

| | |
| --- | --- |
| Key | `web.agents` |
| Values | `on` (On), `off` (Off) |
| Default | `on` |
| Levels | Deployment, Server |
| Combines | Off above is a ceiling |

## Web app: channels

The channels pages of the web app. Off hides them for everyone on the team.

| | |
| --- | --- |
| Key | `web.channels` |
| Values | `on` (On), `off` (Off) |
| Default | `on` |
| Levels | Deployment, Server |
| Combines | Off above is a ceiling |

## Web app: routines

The routines pages of the web app. Off hides them for everyone on the team.

| | |
| --- | --- |
| Key | `web.routines` |
| Values | `on` (On), `off` (Off) |
| Default | `on` |
| Levels | Deployment, Server |
| Combines | Off above is a ceiling |

## Web app: credits

The credits pages of the web app. Off hides them for everyone on the team.

| | |
| --- | --- |
| Key | `web.credits` |
| Values | `on` (On), `off` (Off) |
| Default | `on` |
| Levels | Deployment, Server |
| Combines | Off above is a ceiling |

## Web app: integrations

The integrations pages of the web app. Off hides them for everyone on the team.

| | |
| --- | --- |
| Key | `web.integrations` |
| Values | `on` (On), `off` (Off) |
| Default | `on` |
| Levels | Deployment, Server |
| Combines | Off above is a ceiling |

## Watches

The agent can watch a page or feed and start a conversation when it changes. Off removes the watch tools here and stops the watches made here.

| | |
| --- | --- |
| Key | `watches` |
| Values | `on` (On), `off` (Off) |
| Default | `on` |
| Levels | Deployment, Server, Agent, Channel |
| Combines | Off above is a ceiling |
| Deprecated | `DAIMON_WATCHES__ENABLED` |

## Routines

The agent can set up and run routines: tasks that run on a schedule. Off removes the routine tools here.

| | |
| --- | --- |
| Key | `routines` |
| Values | `on` (On), `off` (Off) |
| Default | `on` |
| Levels | Deployment, Server, Agent, Channel |
| Combines | Off above is a ceiling |

## Long jobs

The agent can start a long job that keeps working after the chat moves on. Off removes the long-job tools here.

| | |
| --- | --- |
| Key | `long_jobs` |
| Values | `on` (On), `off` (Off) |
| Default | `on` |
| Levels | Deployment, Server, Agent |
| Combines | Off above is a ceiling |

## Cloud computers

The agent can start a short-lived cloud computer to run code and keep files. Off removes the computer tools here.

| | |
| --- | --- |
| Key | `compute` |
| Values | `on` (On), `off` (Off) |
| Default | `on` |
| Levels | Deployment, Server, Agent |
| Combines | Off above is a ceiling |
| Deprecated | `DAIMON_COMPUTE__ENABLED` |

## Notebooks

The agent can publish and manage notebooks. Off removes the notebook tools here.

| | |
| --- | --- |
| Key | `notebooks` |
| Values | `on` (On), `off` (Off) |
| Default | `on` |
| Levels | Deployment, Server, Agent, Channel |
| Combines | Off above is a ceiling |

## Report publishing

The agent can publish a report as a web page and delete it later. Off removes the report tools here.

| | |
| --- | --- |
| Key | `report_publish` |
| Values | `on` (On), `off` (Off) |
| Default | `on` |
| Levels | Deployment, Server, Agent, Channel |
| Combines | Off above is a ceiling |

## Video transcripts

The agent can fetch the transcript of a video. Off removes that tool here.

| | |
| --- | --- |
| Key | `media` |
| Values | `on` (On), `off` (Off) |
| Default | `on` |
| Levels | Deployment, Server, Agent, Channel |
| Combines | Off above is a ceiling |

## Microsoft 365

People can connect a Microsoft account and the agent can search, read, write and send with it. Off removes the Microsoft tools here.

| | |
| --- | --- |
| Key | `microsoft_365` |
| Values | `on` (On), `off` (Off) |
| Default | `on` |
| Levels | Deployment, Server, Agent |
| Combines | Off above is a ceiling |

## Chart images in answers

Charts the agent makes come back inside its answer to a connected app as pictures the app can see. Off sends the links only, for an app that cannot take pictures.

| | |
| --- | --- |
| Key | `artifact_images` |
| Values | `on` (On), `off` (Off) |
| Default | `on` |
| Levels | Deployment, Server |
| Combines | Off above is a ceiling |
| Deprecated | `DAIMON_ARTIFACTS__EMBED_IMAGES` |

## Ask a person

Answers offer a way to ask the Daimon team for help, which posts the request in the operator's support channel. Off removes the button and the form. The operator sets the channel and each person's credits.

| | |
| --- | --- |
| Key | `support_escalation` |
| Values | `on` (On), `off` (Off) |
| Default | `on` |
| Levels | Deployment, Server |
| Combines | Off above is a ceiling |

## GitHub access

The agent can ask for access to GitHub repositories, connect an account and pick its working repository. Off removes those tools here.

| | |
| --- | --- |
| Key | `github` |
| Values | `on` (On), `off` (Off) |
| Default | `on` |
| Levels | Deployment, Server, Agent, Channel |
| Combines | Off above is a ceiling |

## Lead intake

The agent can take in sales leads and look them up. Off removes the lead tools here.

| | |
| --- | --- |
| Key | `lead_intake` |
| Values | `on` (On), `off` (Off) |
| Default | `on` |
| Levels | Deployment, Server, Agent |
| Combines | Off above is a ceiling |

## Long job dollar cap

The most one long job may spend. The smallest setting wins, and the team's long-job policy still applies.

| | |
| --- | --- |
| Key | `long_jobs_max_usd` |
| Values | `20` ($20), `10` ($10), `5` ($5), `2` ($2), `1` ($1) |
| Default | `20` |
| Levels | Deployment, Server, Agent |
| Combines | Lowest wins |
| Deprecated | `DAIMON_LONG_JOBS__MAX_USD` |

## Long job time limit

The longest one long job may run. The smallest setting wins, and the team's long-job policy still applies.

| | |
| --- | --- |
| Key | `long_jobs_max_deadline` |
| Values | `70` (70 minutes), `60` (60 minutes), `30` (30 minutes), `15` (15 minutes) |
| Default | `70` |
| Levels | Deployment, Server, Agent |
| Combines | Lowest wins |
| Deprecated | `DAIMON_LONG_JOBS__MAX_DEADLINE_MINUTES` |

## Posting to others

The agent can post where other people are: other channels, new threads and direct messages. Off keeps it to the conversation it is in.

| | |
| --- | --- |
| Key | `outbound_reach` |
| Values | `on` (On), `off` (Off) |
| Default | `on` |
| Levels | Deployment, Server, Agent, Channel |
| Combines | Off above is a ceiling |

## New threads per reply

How many new threads or forum posts outside its own channel, or tagging someone else, one reply may open without approval. The smallest setting wins.

| | |
| --- | --- |
| Key | `outbound_threads` |
| Values | `2` (2), `1` (1), `0` (None) |
| Default | `2` |
| Levels | Deployment, Server, Agent, Channel |
| Combines | Lowest wins |
| Deprecated | `DAIMON_OUTBOUND__MAX_THREADS` |

## Places per reply

How many channels, threads and direct-message recipients outside its own conversation one reply may post into without approval. The smallest setting wins.

| | |
| --- | --- |
| Key | `outbound_places` |
| Values | `2` (2), `1` (1), `0` (None) |
| Default | `2` |
| Levels | Deployment, Server, Agent, Channel |
| Combines | Lowest wins |
| Deprecated | `DAIMON_OUTBOUND__MAX_PLACES` |

## People tagged per reply

How many different people one reply may tag without approval. The smallest setting wins.

| | |
| --- | --- |
| Key | `outbound_people` |
| Values | `5` (5), `3` (3), `1` (1), `0` (None) |
| Default | `5` |
| Levels | Deployment, Server, Agent, Channel |
| Combines | Lowest wins |
| Deprecated | `DAIMON_OUTBOUND__MAX_PEOPLE` |

## Post length outside the conversation

The longest post, in characters, a reply may send outside its own conversation. The smallest setting wins.

| | |
| --- | --- |
| Key | `outbound_length` |
| Values | `1500` (1500), `1000` (1000), `500` (500), `250` (250) |
| Default | `1500` |
| Levels | Deployment, Server, Agent, Channel |
| Combines | Lowest wins |
| Deprecated | `DAIMON_OUTBOUND__MAX_LENGTH` |

## Daily spend per model

The most the team may spend on one OpenAI or Gemini model in a day, at list price. The smallest setting wins, and the operator's provider cap still applies.

| | |
| --- | --- |
| Key | `daily_model_cap` |
| Values | `20` ($20), `10` ($10), `5` ($5), `2` ($2), `1` ($1) |
| Default | `20` |
| Levels | Deployment, Server, Agent |
| Combines | Lowest wins |

## Tool safety

A change through a connected tool waits for the person to press Approve. In a routine or other run nobody watches, it is refused unless the operator allowed that tool. Reads always run.

| | |
| --- | --- |
| Key | `tool_safety` |
| Values | `off` (Off), `on` (On) |
| Default | `off` |
| Levels | Deployment |
| Combines | Stricter wins |
| Deprecated | `DAIMON_TOOL_SAFETY__ENABLED` |

## Reach check

Before one reply posts in more places, opens more threads or tags more people than the limits allow, the person approves it on a card. Where no card can be shown, the post is refused.

| | |
| --- | --- |
| Key | `reach_budget` |
| Values | `off` (Off), `on` (On) |
| Default | `on` |
| Levels | Deployment, Server, Agent, Channel |
| Combines | Stricter wins |
| Deprecated | `DAIMON_OUTBOUND__REACH_BUDGET_ENABLED` |

## Built-in tools in routines

Per routine lets an admin turn on built-in tools for one routine. Off keeps them off in every routine, even one an admin turned them on for.

| | |
| --- | --- |
| Key | `unattended_capabilities` |
| Values | `on` (Per routine), `off` (Off) |
| Default | `on` |
| Levels | Deployment, Server, Agent |
| Combines | Stricter wins |

## Routines must say where they post

A routine the agent makes must name where its results go, or it is not made. Existing routines are not changed.

| | |
| --- | --- |
| Key | `require_routine_destination` |
| Values | `off` (Optional), `on` (Required) |
| Default | `off` |
| Levels | Deployment, Server |
| Combines | Stricter wins |
| Deprecated | `DAIMON_MCP__REQUIRE_ROUTINE_DESTINATION` |
