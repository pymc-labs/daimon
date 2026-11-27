- **Daimon deletes old records on a schedule.**
  Once a day the scheduler removes audit events, admin history, finished jobs
  and their output, closed requests, deleted web threads, feedback, expired
  secret share records and stale channel names past their lifetime, and blanks
  old support notes (the request and its credit stay), each set under
  `DAIMON_RETENTION__*` (0 keeps forever). Each rule runs on its own, a restart
  does not repeat the day's run, and the sweep never holds up usage settlement
  or routines; the hourly sweeps of monitored events, Issues page events and
  idle Gemini conversations run as rules of the same sweep. **Behaviour change
  for existing deployments:** the Conversation history kept feature is on at one
  year, which a workspace admin may shorten. A channel thread, private chat or
  web thread idle longer than that, with no routine, watch or follow-up still
  aimed at it, loses its stored history and turn records, and starts a fresh
  session on its next message; set `DAIMON_FEATURES__CONVERSATION_RETENTION=0`
  to keep them. Erasing a person also removes their follow-up tasks, turn
  records, conversation links, model-switch cards and credential requests in
  every workspace. Job cancel records age with the audit events, a watch keeps
  its last-fired time, and `DAIMON_SECURITY_AUDIT_RETENTION_DAYS` still works.
