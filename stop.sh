#!/usr/bin/env bash
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"
docker compose stop
echo "Docker services stopped; persistent Grafana and Prometheus volumes were kept. Ollama was not stopped."
