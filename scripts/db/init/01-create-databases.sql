-- Postgres official images execute *.sql / *.sh files in /docker-entrypoint-initdb.d
-- on first container start (empty data dir). This script creates the dev database
-- (`daimon`, matches POSTGRES_DB) and the test database (`daimon_test`).
-- POSTGRES_DB is created by the entrypoint before this file runs, so we guard with
-- a conditional SELECT/\gexec to stay idempotent if an operator tweaks POSTGRES_DB
-- or re-runs against an existing volume.

SELECT 'CREATE DATABASE daimon'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'daimon')\gexec

SELECT 'CREATE DATABASE daimon_test'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'daimon_test')\gexec
