#!/usr/bin/env sh
# Start the Streamlit frontend for deployment: listens on $HOST (default 0.0.0.0) and
# $PORT (Render assigns it; default 8501). API_BASE_URL must point at the backend.
# PYTHON selects the interpreter (default: the first `python` on PATH, i.e. the active venv).
set -eu
exec "${PYTHON:-python}" -m streamlit run frontend/streamlit_app.py \
  --server.address "${HOST:-0.0.0.0}" \
  --server.port "${PORT:-8501}" \
  --server.headless true
