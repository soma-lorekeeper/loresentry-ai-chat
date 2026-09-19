#!/bin/sh
set -e

export FLYWAY_URL="jdbc:postgresql://${DB_HOST:-localhost}:${DB_PORT:-5432}/${DB_NAME:-ai_chat}"
export FLYWAY_USER="${DB_USERNAME:-ai_chat_svc}"
export FLYWAY_PASSWORD="${DB_PASSWORD:-}"
export FLYWAY_LOCATIONS="filesystem:/app/db/migration"

flyway migrate

exec "$@"
