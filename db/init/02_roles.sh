#!/bin/sh
# Creates the read-only application role. Runs once on first init of the data volume.
# SECURITY-CRITICAL: this is the real safety net behind the SQL validator.
set -eu

psql -v ON_ERROR_STOP=1 \
     --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
     -v ro_user="$POSTGRES_READONLY_USER" \
     -v ro_pass="$POSTGRES_READONLY_PASSWORD" \
     -v timeout_ms="${STATEMENT_TIMEOUT_MS:-5000}" \
     -v db_name="$POSTGRES_DB" <<'EOSQL'

-- role: login only, no privileges beyond what is granted below
CREATE ROLE :"ro_user" LOGIN PASSWORD :'ro_pass'
    NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;

-- nobody but the owner may create objects in public
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT  USAGE  ON SCHEMA public TO :"ro_user";
GRANT  CONNECT ON DATABASE :"db_name" TO :"ro_user";

-- session guards, applied on every connection of this role
ALTER ROLE :"ro_user" SET default_transaction_read_only = on;
ALTER ROLE :"ro_user" SET statement_timeout = :'timeout_ms';
ALTER ROLE :"ro_user" SET idle_in_transaction_session_timeout = '10000';
ALTER ROLE :"ro_user" SET lock_timeout = '2000';
ALTER ROLE :"ro_user" CONNECTION LIMIT 10;

-- table-level SELECT on tables without PII
GRANT SELECT ON products, orders, order_items, refunds, subscriptions TO :"ro_user";

-- column-level SELECT: PII columns (email, phone, card_last4) are deliberately NOT granted
GRANT SELECT (id, full_name, country, marketing_opt_in, is_test_account, created_at)
    ON customers TO :"ro_user";
GRANT SELECT (id, order_id, amount_cents, method, status, paid_at)
    ON payments TO :"ro_user";
EOSQL
