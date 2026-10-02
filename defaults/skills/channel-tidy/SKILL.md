---
name: channel-tidy
description: Keep channels clean after posting with send_message or create_thread. Use when a post of yours is out of date, wrong, duplicated or failed, before posting a correction or a new status, and when a thread you opened is finished.
---

# Channel tidy

You can change only what you posted yourself with `send_message` or
`create_thread`. Your normal chat replies, people's messages, other agents'
posts and daimon's own cards are not yours to change, and the tools refuse
them.

- Correcting or updating a post: call `edit_message` on it. Do not post a
  second copy.
- A status or progress post you sent earlier: edit it as the work moves, and
  delete it when it no longer helps anyone.
- A failed attempt, a duplicate or a post in the wrong place: delete it with
  `delete_message`, then post once in the right place.
- A thread you opened that is done: `archive_thread` on Discord. Use
  `delete_thread` only for a thread of yours that nobody else wrote in.

Pass this turn's `origin_context_id` to every call. Each turn allows 10 edits
or deletes and each hour 40, so tidy what you just made, not old history.
Never use these tools to remove a record someone may need, such as an answer
a person replied to. If a call is refused, tell the person and do not retry
another way.
