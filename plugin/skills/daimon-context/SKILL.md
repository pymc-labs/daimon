---
name: daimon-context
description: Use when the user asks about team context, project status, decisions, or discussions that live in Slack or Discord, or names a daimon. Routes the question to the right daimon servers and merges their answers.
---

# daimon-context

Each connected `daimon-*` MCP server is one chat platform. `daimon-slack` reaches the
user's Slack workspace; `daimon-discord` reaches every Discord server they share with a
daimon install. A daimon answers as the user: it reads only channels they can see and
spends that workspace's credit.

## Procedure

1. When the question mentions Slack, Discord, the team, a channel, or a daimon by name,
   call `list_daimons` on each connected server. Every entry has an `id`, a `name`, and a
   `workspace`. Names repeat across workspaces; `id` does not.
2. Pick the daimons whose `workspace` or `role_summary` plausibly holds the answer. Do not
   ask all of them by default. Each workspace is billed separately.
3. Call `ask(daimon_id, message)` on the chosen daimons in parallel. Phrase the message as
   a question the daimon can answer from its own platform.
4. Merge the replies. State which platform and workspace each fact came from.
5. For a follow-up on the same topic, pass the previous result's `handle` to `ask` so the
   daimon keeps its context and no new session is opened.

## Failure modes

- A server reports missing authentication: tell the user to run `/mcp` and authenticate
  that server. Do not retry the call.
- `ask` times out after about two minutes: the error carries a `handle`. Call
  `get_session(daimon_id, handle)` until `idle`, then `list_events` to read the reply.
  Do not resend the question.
- One daimon fails while others answer: report the partial result and name the daimon
  that did not answer.
- `list_daimons` returns an empty list: daimon is not installed in any workspace the user
  belongs to on that platform. Say so; there is nothing to retry.
