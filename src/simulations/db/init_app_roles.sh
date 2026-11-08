#!/bin/sh
# Создаёт отдельные учётные записи приложений и не оставляет им права суперпользователя.
set -eu

: "${BANK_DB_PASSWORD:?BANK_DB_PASSWORD is required}"
: "${INVEST_DB_PASSWORD:?INVEST_DB_PASSWORD is required}"
: "${DIGITAL_DB_PASSWORD:?DIGITAL_DB_PASSWORD is required}"

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname postgres <<EOSQL
DO \$\$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'bank_app') THEN
        CREATE ROLE bank_app LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD '${BANK_DB_PASSWORD}';
    ELSE
        ALTER ROLE bank_app WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD '${BANK_DB_PASSWORD}';
    END IF;
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'invest_app') THEN
        CREATE ROLE invest_app LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD '${INVEST_DB_PASSWORD}';
    ELSE
        ALTER ROLE invest_app WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD '${INVEST_DB_PASSWORD}';
    END IF;
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'digital_app') THEN
        CREATE ROLE digital_app LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD '${DIGITAL_DB_PASSWORD}';
    ELSE
        ALTER ROLE digital_app WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD '${DIGITAL_DB_PASSWORD}';
    END IF;
END
\$\$;
GRANT CONNECT ON DATABASE bank_db TO bank_app;
GRANT CONNECT ON DATABASE invest_db TO invest_app;
GRANT CONNECT ON DATABASE digital_db TO digital_app;
REVOKE CONNECT ON DATABASE bank_db FROM PUBLIC;
REVOKE CONNECT ON DATABASE invest_db FROM PUBLIC;
REVOKE CONNECT ON DATABASE digital_db FROM PUBLIC;
EOSQL

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname bank_db <<EOSQL
GRANT USAGE, CREATE ON SCHEMA public TO bank_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO bank_app;
DO \$\$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.tables
        WHERE table_schema = 'public' AND table_name = 'auto_payments'
    ) THEN
        ALTER TABLE auto_payments ADD COLUMN IF NOT EXISTS idempotency_key VARCHAR;
        CREATE UNIQUE INDEX IF NOT EXISTS ix_auto_payments_idempotency_key
            ON auto_payments (idempotency_key);
    END IF;
END
\$\$;
EOSQL

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname invest_db <<EOSQL
GRANT USAGE, CREATE ON SCHEMA public TO invest_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO invest_app;
EOSQL

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname digital_db <<EOSQL
GRANT USAGE, CREATE ON SCHEMA public TO digital_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO digital_app;
EOSQL
