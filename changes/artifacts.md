- **Agents can publish interactive pages, and watch them on Discord.** An agent
  hosts one HTML file at `/a/<id>/<slug>` in the web app, shared with the
  workspace, named people or anyone with a link, with an optional expiry. A page
  can keep shared data, send events and let viewers ask the agent, each off until
  turned on. Eight `artifact_*` tools publish, list, read and write a page's
  data, fetch its source, delete and transfer it; routines may only list, query
  and write. `create_watch(source="page_event", page=...)` wakes an agent on a
  page's events from any chat or the web app, and `rotate_watch_key` no longer sends a key for
  such a watch. The new Artifacts feature is on wherever the web app is, and the
  seeded agent gets an `artifacts` skill. Sharers can invite an email address
  as a page-only visitor, managers can limit viewers to their own documents per
  collection, Claude Code plugin logins publish as their person, and the limits
  are `DAIMON_ARTIFACT_PAGES__*` settings. See the Artifacts page.
