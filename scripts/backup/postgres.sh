#!/bin/sh
# libpq PGHOST/PGPORT/PGUSER/PGPASSWORD/PGDATABASE select the database.
set -eu
umask 077
mode=${1:-}
archive=${2:-}
[ -n "$archive" ] || { echo 'usage: postgres.sh backup|restore ARCHIVE_DIRECTORY' >&2; exit 2; }
case "$mode" in
  backup)
    # mkdir refuses overwrites and gives each run its own private directory.
    mkdir -m 700 "$archive"
    trap 'rm -f "$archive/database.dump.partial"' EXIT HUP INT TERM
    pg_dump --format=custom --no-owner --no-privileges --file="$archive/database.dump.partial"
    mv "$archive/database.dump.partial" "$archive/database.dump"
    (cd "$archive" && sha256sum database.dump > SHA256SUMS)
    ;;
  restore)
    [ -n "${PGDATABASE:-}" ] && [ "${DAIMON_RESTORE_DATABASE:-}" = "$PGDATABASE" ] || {
      echo 'Set DAIMON_RESTORE_DATABASE to the target PGDATABASE to confirm restore.' >&2; exit 2;
    }
    # Validate exactly the expected dump, never paths read from an untrusted manifest.
    expected=$(cut -d ' ' -f 1 "$archive/SHA256SUMS")
    actual=$(sha256sum "$archive/database.dump" | cut -d ' ' -f 1)
    [ "$expected" = "$actual" ] || { echo 'Backup checksum mismatch.' >&2; exit 1; }
    # Restore only to a freshly created DB. Never clean/drop an existing deployment.
    objects=$(psql -XAt --set=ON_ERROR_STOP=1 --command="SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname NOT IN ('pg_catalog','information_schema') AND n.nspname NOT LIKE 'pg_toast%'")
    [ "$objects" = 0 ] || { echo 'Restore requires an empty database.' >&2; exit 1; }
    pg_restore --dbname="$PGDATABASE" --single-transaction --exit-on-error \
      --no-owner --no-privileges "$archive/database.dump"
    ;;
  *) echo 'Expected backup or restore.' >&2; exit 2 ;;
esac
