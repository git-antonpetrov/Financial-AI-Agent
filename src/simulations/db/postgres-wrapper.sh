#!/bin/sh
# Запускает PostgreSQL и повторно применяет роли приложений на уже инициализированном томе.
set -eu

PGDATA="${PGDATA:-/var/lib/postgresql/data}"

if [ -f /certs/postgres.key ]; then
    mkdir -p /tmp/pg_certs
    cp /certs/postgres.key /tmp/pg_certs/postgres.key
    chmod 600 /tmp/pg_certs/postgres.key
    chown postgres:postgres /tmp/pg_certs/postgres.key
fi

if [ -s "${PGDATA}/PG_VERSION" ]; then
    docker-entrypoint.sh "$@" &
    child=$!
    ready=0
    attempt=0
    while [ "$attempt" -lt 60 ]; do
        if pg_isready -q; then
            ready=1
            break
        fi
        attempt=$((attempt + 1))
        sleep 1
    done
    if [ "$ready" -ne 1 ]; then
        echo "PostgreSQL не стал доступен для повторного применения ролей" >&2
        kill "$child" 2>/dev/null || true
        wait "$child" || true
        exit 1
    fi
    if [ -f /docker-entrypoint-initdb.d/02-init_app_roles.sh ]; then
        if ! sh /docker-entrypoint-initdb.d/02-init_app_roles.sh; then
            kill "$child" 2>/dev/null || true
            wait "$child" || true
            exit 1
        fi
    fi
    wait "$child"
else
    exec docker-entrypoint.sh "$@"
fi
