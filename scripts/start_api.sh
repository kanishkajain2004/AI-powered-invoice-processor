#!/usr/bin/env sh
# Start the FastAPI backend for deployment: listens on $HOST (default 0.0.0.0) and
# $PORT (Render assigns it; default 8000). One worker, because SQLite has one writer.
# PYTHON selects the interpreter (default: the first `python` on PATH, i.e. the active venv).
set -eu
exec "${PYTHON:-python}" -m uvicorn app.main:app \
  --host "${HOST:-0.0.0.0}" \
  --port "${PORT:-8000}" \
  --workers 1 \
  --proxy-headers \
  --forwarded-allow-ips "${FORWARDED_ALLOW_IPS:-*}"
