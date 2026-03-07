#!/bin/bash

mkdir -p /app/logs
LOG=/app/logs/startup.log

echo "=== $(date) startup ===" | tee -a "$LOG"
echo "Running database migrations..." | tee -a "$LOG"

alembic upgrade head 2>&1 | tee -a "$LOG"
ALEMBIC_EXIT=${PIPESTATUS[0]}

if [ "$ALEMBIC_EXIT" -ne 0 ]; then
    echo "ERROR: alembic migration failed with exit code $ALEMBIC_EXIT" | tee -a "$LOG"
    exit "$ALEMBIC_EXIT"
fi

echo "Migrations complete." | tee -a "$LOG"
echo "Starting tradebot server..." | tee -a "$LOG"

uvicorn scorched.main:app --host "${HOST:-0.0.0.0}" --port "${PORT:-8000}" 2>&1 | tee -a "$LOG"
exit ${PIPESTATUS[0]}
