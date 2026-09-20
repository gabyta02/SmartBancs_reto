#!/bin/sh
set -e

psql \
  --username "$POSTGRES_USER" \
  --dbname "$POSTGRES_DB" \
  <<-EOSQL

DO \$\$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_roles
        WHERE rolname = '${DB_APP_USER}'
    ) THEN
        CREATE ROLE ${DB_APP_USER}
        LOGIN
        PASSWORD '${DB_APP_PASSWORD}';
    END IF;
END
\$\$;

EOSQL