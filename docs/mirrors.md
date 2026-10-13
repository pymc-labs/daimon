# Mirrors

A mirror copies an agent's agent-chat questions and answers into a Discord
channel or thread of the same server, so a team can follow what people ask
an agent from an MCP client without being in those conversations. It is off
unless `DAIMON_MIRRORS__ENABLED=true`
([configuration.md](configuration.md#mirrors)).

## What is copied

Every answered `ask` to the mirrored agent, through an agent key or the hub
(see [mcp-tools.md](mcp-tools.md#agent_chat)). Turns run with `start_turn` or
`continue_turn` and read back with `get_session` are not copied yet. Copied: the question and the final
answer, quoted, with secrets redacted and long text cut at `max_answer_chars`. Tool
calls, their output, files and chart links are never copied. The asker is named by display
name, never by platform id, and marked "(external)" when they are from
another organisation. Posts carry no mentions or link previews, and nothing said in a mirror's
thread starts a turn, even where Daimon follows threads on its own.

In a channel, each conversation gets one thread, opened on a short header and
reused for its later questions; a thread deleted, locked or closed to the
bot is replaced. In a thread,
posts go straight in. The `ask` result's `shared_to` lists the names of the
mirrors that will post it, so the asker sees them: name a mirror for the
place it posts to.

## Who decides

Admins, with the admin-only tools `create_mirror`, `list_mirrors` and
`remove_mirror`. Each call to `create_mirror` and `remove_mirror` is written to
the security audit trail.

| Source conversation | Copied when |
| --- | --- |
| An agent key minted in a channel | that channel's rules allow it (below) |
| A person's own (unbound key, hub) | the mirror has recorded consent |

Recorded consent is the `consent_note` an admin gives: who agreed and how.
It is taken only from a chat conversation where the admin presses Approve on
a card (approval cards on, [configuration.md](configuration.md#tool-safety)),
never from an agent key, a routine or another headless call. Daimon stores it
with the admin's account and the time, writes it to the audit trail, and
shows it in `list_mirrors`. Without it, a person's own
conversations are not queued at all.

## Channel rules

A mirror is checked against the destination's and the source's channel rules
([permissions.md](permissions.md)) when it is created and again before every
post, with the live rules.

| Refusal | When |
| --- | --- |
| `writers_none` | the destination lets nobody post |
| `not_a_reader` | the source conversation may not be read from the destination (a `readers: inside` or `own` channel's sessions stay in it) |
| `own_agents_only` | the destination is kept to its own agents (`readers: own`) and the agent is not one of them, or the agent is a home's own and the destination is outside that home |

An agent's own home is the main use: its agent-chat Q&A posted into its own
private channel. `runs_in` does not limit mirroring.

## Delivery

`ask` queues one row per matching mirror in `mirror_deliveries` and returns;
it never fails because of a mirror. The Discord bot drains the queue every
`poll_interval_s`, one post per conversation at a time so threads keep their
order. Delivery is at most once: a post whose lease ran out, or one older than
`max_pending_age_s`, is skipped rather than retried, since it may already have
landed. Rules are checked against the agent's names at delivery, so a renamed
agent meets its current rules. A missing destination or agent, a refusal of the
destination or a bot that may not post pauses the mirror; a refusal of one conversation alone, or a passing
Discord error, skips that post only; `list_mirrors` shows why, and calling `create_mirror` again with the
same name resumes it. A settled row keeps no question or answer. The
scheduler settles stale, interrupted and paused rows on every tick, whether
or not mirrors or the bot's poller are on, archived workspaces included.

Deleting a person's data (`/privacy`) deletes their queued and settled rows;
messages already posted stay. Slack and Teams destinations are not built yet:
`create_mirror` refuses them.
