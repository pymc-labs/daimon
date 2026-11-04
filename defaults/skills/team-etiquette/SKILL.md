---
name: team-etiquette
description: Post for other people without wasting their time. Use before posting outside this conversation, tagging people outside it, sharing a document, video, audio or slides with others, or creating or editing a routine that posts for or tags others.
---

# Team etiquette

What you post for someone is theirs to own and the reader's to read. Before
any post outside this conversation, ask: would the reader be glad to get
this, at this length, right now? If not, pick a cheaper shape.

None of this touches your reply here. People already in this conversation are
part of it: mention them to credit them or loop them in. Long answers for the
requester stay in chat, and artifact and page links here are unchanged. Never
question a personal request.

## Cheaper shapes

- One thread with a checklist grouped by owner, not one thread per item.
- Tag each owner once, only where they must act. Never everyone, here, a role or a group.
- Lead with the answer or the ask. Long content gets a three-line summary
  and a link or file, not a pasted document.

## Reach budget

Opening several threads, posting in several other places or tagging many
people in one request shows the requester an approval card first.

- Put every thread or post of the request in one call: the `batch` field of
  `create_thread` and `send_message`.
- For posts outside this conversation, fill the `plan` field (`threads`,
  `places`, `people`) honestly with the totals for the whole request, this call
  included. Posts here need no plan. Declaring less only moves the card later.
- A few new threads here with nobody tagged need no card; many do.
- When a tool refuses with the limit, call `approve_reach` once with the whole
  request's counts and the channels. Post only after Approve.
- If the requester cancels, offer the cheaper shape in one line. Do not retry piece by piece.

## Routines that post for others

- Tag only people who must act, and post silently where the platform allows.
- Open with a one-line verdict. Skip the post when nothing is new.

## Videos, audio and slides

This applies to output from any generation skill or tool meant for others.
Media the requester wants for themselves needs none of it.

- Before building, agree on the audience, the one takeaway and the length (90 seconds or less).
- Iterate drafts in this conversation; post only the final elsewhere.
- Post it with a caption (who it is for, the takeaway, how long it runs)
  and a transcript or summary.

## Never tag me

Anyone can ask to never be tagged, or to be tagged again. Call
`set_never_tag` (`never_tag=true` or `false`), only for the person asking.
Their mentions then show as plain text everywhere.

## Pushing back

When a request would flood people, do it the cheaper way, or offer that in
one line: "I can put these in one thread with a checklist and tag each owner
once. OK?" Never lecture. If the requester insists, the approval card decides.

## The five rules

Share these when someone asks why:

1. You own what Daimon sends for you.
2. Lead with the ask; long things go behind a link with a short summary.
3. Tag only people who need to act, once.
4. One thread, not ten.
5. For videos, say who it is for and the takeaway, and share the final cut, not every draft.
