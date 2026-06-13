#!/bin/bash

cd "$(dirname "$0")"

export DB_HOST="${DB_HOST:-localhost}"
export DB_PORT="${DB_PORT:-3306}"
export DB_USER="${DB_USER:-root}"
export DB_PASSWORD="${DB_PASSWORD:-password}"
export DB_NAME="${DB_NAME:-btc_tracker}"

echo "Starting api-server..."
echo "MySQL: $DB_USER@$DB_HOST:$DB_PORT/$DB_NAME"

exec ./api-server "$@"
