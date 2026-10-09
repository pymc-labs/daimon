"""Durable state: the `StateStore` protocol and the pure rules behind it.

Each module holds one record's rules as pure functions over contract values
(no I/O, no clock), so the in-memory store here and Daimon's Postgres store
make the same decisions. `memory` is the restartable test store.
"""
