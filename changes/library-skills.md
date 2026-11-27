- **Coding keys, the hub and outside clients can read the skill library.** The
  workspace feature `library_skills` (on by default, and idle until the library
  holds skills) adds `list_library_skills` and `read_library_skill`; a scoped
  skill shows only to callers whose data reads include its scope, and in chat
  only when everyone who can read the room holds it. An agent or channel
  carrying a scoped skill loads it only in DMs, the web chat and Discord rooms
  where everyone present holds its scope. Admins add a repository's skills and
  set a skill's scope on the web or in chat (`add_library_repository`,
  `get_skill_scope`, `set_skill_scope`); declared `skill_repos` add skills too
  ([Data sources](docs/data-sources.md)).
