- **Data sources are on by default and inert until used.**
  `DAIMON_FEATURES__DATA_SOURCES=off` turns them off, and no data tool is
  offered until a workspace has a source. A data-reading agent no longer
  starts a fresh session each turn, except when held, or loses long-term
  memory: it and other agents ask each other only when both read data, writes
  out still ask first, and a memory saved after a data read shows only to
  people who could read that data. Workspace admins manage data access, and
  may grant themselves a scope or link an account to themselves (audited).
  Chat adds a source or refreshes its credential through a private form only
  the asker opens, and in a room sends emails, sign-ins and rules by direct
  message. Redaction sets take `at: storage` or `at: answer`, reach SQL and
  file text best effort, and apply to their own sources. Teams rooms with a
  guest, another organisation's member or a shared channel get no data; others
  get public data until the app (0.6.3) may read their members, per team or
  for the whole tenant ([Data
  sources](docs/data-sources.md#who-sees-an-answer)).
