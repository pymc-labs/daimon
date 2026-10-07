---
name: channel-tidy
description: Keep channels clean after posting. Use when a post, reply or status card of yours is out of date, empty, wrong, duplicated or failed, before posting a correction or a new status, when a thread you opened is finished, and when someone asks you to tidy a thread.
---

# Channel tidy

You can change only what you posted yourself: messages from `send_message`
or `create_thread`, and on Discord your own chat replies and status cards
and the thread opened when someone mentioned you. People's messages, other
agents' posts and daimon's other cards are not yours to change, and the
tools refuse them.

- Correcting or updating a post: call `edit_message` on it. Do not post a
  second copy.
- A status or progress post you sent earlier: edit it as the work moves, and
  delete it when it no longer helps anyone.
- An empty status card, a failed attempt, a duplicate or a post in the wrong
  place: delete it with `delete_message`, then post once in the right place
  if needed.
- "Tidy this thread": read the thread, delete your own empty cards and stale
  or duplicated replies one by one, and keep answers people relied on.
  `delete_thread` removes all your posts in it at once. The status card of
  the turn you are in cannot be changed until the turn ends. The thread's
  first message, an empty "thread starter" echo Discord adds, is not one of
  your posts or cards: leave it out of what you tidy and report.
- A thread you opened that is done: `archive_thread` on Discord. The thread
  you are answering in is archived when your turn ends, so say it will be. Use
  `delete_thread` to remove your own posts. Discord keeps the thread and
  other people's messages; Slack refuses threads with other people's replies.
  Each deleted message counts against the limits; a refusal stops the batch.

A reply or card of yours can be tidied only when the person asking started
that turn, opened the thread with you, or is a server admin; the thread
opened from a mention only when its opener or a server admin asks. Pass this turn's `origin_context_id` to
every call. Each turn allows 10 edits or deletes and each hour 40, so tidy
what you just made, not old history. Never use these tools to remove a
record someone may need, such as an answer a person replied to. If a call
is refused, tell the person and do not retry another way.
