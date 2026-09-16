#!/usr/bin/env bash
# 本机原生启动：bash run.sh [port]
set -euo pipefail
cd "$(dirname "$0")"
PORT="${1:-8000}"
export NIANHUA_DB="${NIANHUA_DB:-$PWD/nianhua.db}"
exec .venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port "$PORT"
