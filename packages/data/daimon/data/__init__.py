"""The data store: synced sources, their content and search, in their own Postgres schemas.

Optional package. Core never imports it; it plugs in through `daimon.core.ports` (see
`ports.register`), and everything here stays inert until the `data_sources` feature is on.
"""
