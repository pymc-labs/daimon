- **The groundwork for a data store ships, switched off.** A release migration
  creates the `data` and `data_views` Postgres schemas, the store's tables and
  four `daimon_data_*` roles; the `data_sources` feature
  (`DAIMON_FEATURES__DATA_SOURCES`) stays off and nothing reads them yet. A
  database user that cannot create them logs `data_store_skipped` and Daimon
  starts anyway; `daimon data init` sets the store up later, and `--repair`
  puts back its grants after a restore. The default Postgres image is
  unchanged; `docker-compose.data.yml` swaps in one with pgvector
  ([Data sources](docs/data-sources.md)).
