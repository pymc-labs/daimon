- **Data sources can go stale, be read live, be declared in a file and be checked before they publish.**
  A source past its freshness ceiling is hidden until it syncs; a new source
  publishes only after a clean comparison with its own permissions.
  `DAIMON_DATA__DECLARED` names a file of sources, scopes and grants that
  Daimon applies every 10 minutes without deleting, and `daimon data
  access-check` explains who can read a record ([Data
  sources](docs/data-sources.md)).
