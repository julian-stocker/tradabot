#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Container entrypoint: wait for the database, apply migrations, then exec CMD.
# ---------------------------------------------------------------------------
set -euo pipefail

MAX_WAIT="${TRADABOT_DB_WAIT_SECONDS:-60}"

wait_for_database() {
    local waited=0
    echo "[entrypoint] waiting for the database (up to ${MAX_WAIT}s)..."
    until python -c "
import asyncio, sys
from sqlalchemy import text
from app.core.config import get_settings
from app.db.session import create_engine

async def check() -> None:
    engine = create_engine(get_settings())
    try:
        async with engine.connect() as connection:
            await connection.execute(text('SELECT 1'))
    finally:
        await engine.dispose()

try:
    asyncio.run(check())
except Exception as exc:
    print(f'  not ready: {type(exc).__name__}', file=sys.stderr)
    sys.exit(1)
" 2>/dev/null; do
        waited=$((waited + 2))
        if [ "${waited}" -ge "${MAX_WAIT}" ]; then
            echo "[entrypoint] database did not become ready within ${MAX_WAIT}s" >&2
            exit 1
        fi
        sleep 2
    done
    echo "[entrypoint] database is ready"
}

# Migrations run on start so `docker compose up` yields a working system with no
# manual step. For a multi-replica deployment this belongs in a separate job --
# concurrent Alembic runs race on the version table.
if [ "${TRADABOT_SKIP_MIGRATIONS:-false}" != "true" ]; then
    wait_for_database
    echo "[entrypoint] applying migrations..."
    alembic upgrade head
    echo "[entrypoint] migrations applied"
fi

exec "$@"
