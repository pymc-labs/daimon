- **Agents can read Daimon's own docs and source.** Turn on "Knows Daimon"
  (`self_knowledge`, Off by default) for a workspace, agent, channel or thread,
  and four read-only tools (`read_daimon_docs`, `search_daimon_docs`,
  `read_daimon_source`, `search_daimon_source`) read and search the docs and
  `packages/` code of the release the agent runs on, naming the repository and
  commit. Where Web app is Off, the web app's pages and code are left out. New
  pages cover [how Daimon works](docs/how-daimon-works.md),
  [approvals](docs/approvals.md) and [helpers and handoffs](docs/helpers.md);
  every feature names its page, `docs/llms.txt` indexes the site, and CI fails
  on time words, stale code names and web text outside its markers.
