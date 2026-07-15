#!/bin/sh
# Run idempotent first-boot seeding, then exec the per-process command from
# fly.toml [processes]. See docs/deploy/production.md for context.
#
# Skip `daimon defaults apply` when invoked as `alembic ...` — that path is
# the Fly release_command VM, which runs migrations and exits. Seeding belongs
# on the long-running process VMs after the schema is up.
set -e

if [ "$1" != "alembic" ]; then
    daimon defaults apply
fi

exec "$@"
