#!/bin/sh
# Fully isolated local restore drill. Does not use Compose or deployment env.
set -eu
container="daimon-ops017-drill-$$"
root=$(CDPATH='' cd -- "$(dirname "$0")/../.." && pwd)
trap 'docker rm -f "$container" >/dev/null 2>&1 || true' EXIT HUP INT TERM
docker run -d --name "$container" --network none \
  -e POSTGRES_HOST_AUTH_METHOD=trust -e POSTGRES_DB=source \
  -v "$root/scripts/backup:/backup-tools:ro" postgres:18-alpine >/dev/null
for i in $(seq 1 60); do
  if docker exec "$container" pg_isready -U postgres -d source >/dev/null 2>&1; then break; fi
  sleep 1
done
docker exec -i "$container" psql -U postgres -d source -v ON_ERROR_STOP=1 <<'SQL'
CREATE TABLE recovery_probe (id integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY, payload text NOT NULL);
INSERT INTO recovery_probe(payload) VALUES ('credentials survive as ciphertext'), ('routine and usage fixture');
CREATE TABLE alembic_version(version_num varchar(32) PRIMARY KEY);
INSERT INTO alembic_version VALUES ('ops017_drill');
SQL
docker exec -e PGUSER=postgres -e PGDATABASE=source "$container" \
  sh /backup-tools/postgres.sh backup /tmp/backup
docker exec "$container" createdb -U postgres scratch
docker exec -e PGUSER=postgres -e PGDATABASE=scratch -e DAIMON_RESTORE_DATABASE=scratch \
  "$container" sh /backup-tools/postgres.sh restore /tmp/backup
result=$(docker exec "$container" psql -XAt -U postgres -d scratch -c \
  "SELECT string_agg(payload, '|' ORDER BY id) FROM recovery_probe")
[ "$result" = 'credentials survive as ciphertext|routine and usage fixture' ]
version=$(docker exec "$container" psql -XAt -U postgres -d scratch -c 'SELECT version_num FROM alembic_version')
[ "$version" = ops017_drill ]
# Verify identity/sequence state, not just table data.
id=$(docker exec "$container" psql -XAt -U postgres -d scratch -c \
  "INSERT INTO recovery_probe(payload) VALUES ('after restore') RETURNING id" | head -1)
[ "$id" = 3 ]
# Existing databases and missing confirmation must be rejected.
if docker exec -e PGUSER=postgres -e PGDATABASE=scratch -e DAIMON_RESTORE_DATABASE=scratch \
  "$container" sh /backup-tools/postgres.sh restore /tmp/backup; then exit 1; fi
if docker exec -e PGUSER=postgres -e PGDATABASE=scratch \
  "$container" sh /backup-tools/postgres.sh restore /tmp/backup; then exit 1; fi
# Corruption must be rejected before any restore.
docker exec "$container" sh -c 'echo corruption >> /tmp/backup/database.dump'
docker exec "$container" createdb -U postgres empty
if docker exec -e PGUSER=postgres -e PGDATABASE=empty -e DAIMON_RESTORE_DATABASE=empty \
  "$container" sh /backup-tools/postgres.sh restore /tmp/backup; then exit 1; fi
echo 'PASS: scratch restore, data, migration marker, sequence, nonempty/confirmation/checksum guards.'
