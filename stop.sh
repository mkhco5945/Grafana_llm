#!/usr/bin/env bash
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

if [[ -f .run/ai-api.pid ]]; then
  pid="$(cat .run/ai-api.pid 2>/dev/null || true)"
  if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
    kill "$pid" 2>/dev/null || true
  fi
  rm -f .run/ai-api.pid
fi

docker compose stop

echo "Docker services were stopped; persistent Grafana, Prometheus, and .state/ai-chat.sqlite3 chat data were kept. Ollama was not stopped."
