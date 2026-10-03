These functions are copied verbatim from origin/main at
14eaeb1bbcadf10b97beb0cedcf838e394fa3ee3. The source paths are
packages/adapters/{discord,slack,teams}/daimon/adapters/{discord,slack,teams}/
with bot.py for Discord and app.py for Slack and Teams.

test_thread_queue_equivalence.py executes the frozen functions and current
callers with identical scheduled arrivals, completions, faults and cancels.
The recorded effects include admission order, responses, continuation writes,
coalesced content and files, deferred resumes and final queue ownership.

The callbacks stand in for platform I/O and fenced turn execution. Their real
implementations, admission caps, authorization and locks are unchanged.
