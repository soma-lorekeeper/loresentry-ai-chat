#!/bin/bash
set -eo pipefail

export FLYWAY_URL="jdbc:postgresql://${DB_HOST:-localhost}:${DB_PORT:-5432}/${DB_NAME:-ai_chat}"
export FLYWAY_USER="${DB_USERNAME:-ai_chat_svc}"
export FLYWAY_PASSWORD="${DB_PASSWORD:-}"
export FLYWAY_LOCATIONS="filesystem:/app/db/migration"

# Flyway's lines go out as ECS JSON like the app's (plain lines get log.level unknown).
# pipefail keeps flyway's exit code. The URL carries no password; it is only in FLYWAY_PASSWORD.
flyway migrate 2>&1 | python -m app.logs.pipe flyway ai-chat-api

exec "$@"
