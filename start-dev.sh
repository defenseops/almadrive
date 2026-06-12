#!/usr/bin/env bash
set -euo pipefail

HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-8000}"

exec ./.venv/bin/uvicorn api.main:app \
  --reload \
  --host "$HOST" \
  --port "$PORT" \
  --log-level "${LOG_LEVEL:-info}"