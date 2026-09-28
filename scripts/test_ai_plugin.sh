#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

./start.sh

echo
echo "=== AI bridge health ==="
curl -fsS http://127.0.0.1:8010/health
printf '\n'

echo
echo "=== Grafana plugin settings ==="
settings="$(curl -fsS -u admin:admin http://127.0.0.1:3000/api/plugins/mkhco-ai-dashboard-app/settings)"
printf '%s\n' "$settings"
printf '%s' "$settings" | grep -q '"enabled":true' || {
  echo "FAIL AI app is not enabled in Grafana" >&2
  exit 1
}
echo "PASS AI app settings exist and are enabled"

echo
echo "=== Grafana plugin bundle ==="
curl -fsS -o /dev/null http://127.0.0.1:3000/public/plugins/mkhco-ai-dashboard-app/module.js
echo "PASS module.js is served by Grafana"
if grep -q 'react/jsx-runtime' grafana-ai-plugin/dist/module.js; then
  echo "FAIL module.js still depends on react/jsx-runtime, which this Grafana build does not expose to the custom AMD bundle" >&2
  exit 1
fi
echo "PASS module.js does not depend on react/jsx-runtime"

echo
echo "=== Grafana -> plugin proxy -> AI bridge ==="
curl -fsS -u admin:admin \
  http://127.0.0.1:3000/api/plugin-proxy/mkhco-ai-dashboard-app/ai/health
printf '\n'

echo
echo "=== AI PLUGIN SMOKE TEST: PASS ==="
echo "Open: http://localhost:3000/a/mkhco-ai-dashboard-app"
echo "Or open Grafana's command palette and search: Create dashboard with AI"
echo "AI bridge log: $ROOT_DIR/.run/ai-api.log"
