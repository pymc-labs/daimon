- **Artifacts for some workspaces only.** `DAIMON_ARTIFACT_PAGES__WORKSPACES` lists the
  workspaces that have artifacts. Elsewhere the setting, the Artifacts section, pages,
  API, tools (the hub's too), page watches, page-data polls and seeded skill are gone, and `daimon artifacts import`
  refuses. Inside, `artifacts` stays on or off per workspace, agent, channel and thread;
  where it is off or absent, the watch tools never mention pages.
  `DAIMON_WEB__ARTIFACT_CONTENT_SHARED_HOST` lets one workspace serve every page from a
  single content host, with the trade-offs in the artifacts docs. Over https the GitHub
  connect cookies now carry the `__Host-` prefix, so a GitHub connect or link started
  before the upgrade must be started again.
