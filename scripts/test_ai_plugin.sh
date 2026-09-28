#!/usr/bin/env bash
set -uo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

PLUGIN_ID="mkhco-ai-dashboard-app"
GRAFANA_URL="${GRAFANA_URL:-http://127.0.0.1:3000}"
GRAFANA_USER="${GRAFANA_ADMIN_USER:-admin}"
GRAFANA_PASSWORD="${GRAFANA_ADMIN_PASSWORD:-admin}"
mkdir -p .run

fail() {
  local reason="$1" report_path
  printf 'FAIL: %s\n' "$reason" >&2
  report_path="$(bash scripts/diagnose_ai_plugin.sh)" || true
  if [[ -n "$report_path" ]]; then
    printf 'RCA report: %s\n' "$report_path" >&2
  else
    printf 'RCA report: diagnostic collector failed before returning a path\n' >&2
  fi
  exit 1
}

http_status() {
  local url="$1" output="$2"
  curl -sS --max-time 15 -u "$GRAFANA_USER:$GRAFANA_PASSWORD" \
    -o "$output" -w '%{http_code}' "$url" 2>"$output.error" || true
}

phase() { printf '\n[%s/10] %s\n' "$1" "$2"; }

phase 1 'Plugin typecheck and build'
if [[ ! -x grafana-ai-plugin/node_modules/.bin/webpack || ! -x grafana-ai-plugin/node_modules/.bin/tsc ]]; then
  (cd grafana-ai-plugin && npm ci --no-audit --no-fund) \
    >.run/smoke-npm-install.log 2>&1 || fail 'Grafana plugin dependency installation failed'
fi
(cd grafana-ai-plugin && npm run typecheck) || fail 'Grafana plugin typecheck failed'
(cd grafana-ai-plugin && npm run build) || fail 'Grafana plugin webpack build failed'
[[ -f grafana-ai-plugin/dist/module.js ]] || fail 'Webpack did not produce dist/module.js'
echo 'PASS: plugin typecheck and build'

# Start the stack after the explicit build. start.sh still restarts Grafana so
# its in-memory route table reflects the freshly copied plugin.json.
AI_PLUGIN_ALREADY_BUILT=1 AI_PLUGIN_SKIP_PROXY_CHECK=1 ./start.sh \
  || fail 'Local stack or host AI bridge startup failed'

phase 2 'Grafana health'
status="$(http_status "$GRAFANA_URL/api/health" .run/smoke-grafana-health.body)"
[[ "$status" == 200 ]] || fail "Grafana health returned HTTP ${status:-000}"
echo 'PASS: Grafana is healthy'

phase 3 'Plugin registration'
status="$(http_status "$GRAFANA_URL/api/plugins" .run/smoke-plugins.body)"
[[ "$status" == 200 ]] || fail "Grafana plugin list returned HTTP ${status:-000}"
rg -q "\"id\":\"$PLUGIN_ID\"" .run/smoke-plugins.body \
  || fail "Grafana did not register $PLUGIN_ID"
echo 'PASS: plugin is registered'

phase 4 'App setting exists and is enabled'
status="$(http_status "$GRAFANA_URL/api/plugins/$PLUGIN_ID/settings" .run/smoke-settings.body)"
[[ "$status" == 200 ]] || fail "Grafana plugin settings returned HTTP ${status:-000}"
rg -q '"enabled"[[:space:]]*:[[:space:]]*true' .run/smoke-settings.body \
  || fail 'Grafana AI app setting exists but is not enabled'
echo 'PASS: app setting exists and is enabled'

phase 5 'Plugin module serving'
status="$(http_status "$GRAFANA_URL/public/plugins/$PLUGIN_ID/module.js" .run/smoke-module.js)"
[[ "$status" == 200 ]] || fail "Grafana module.js request returned HTTP ${status:-000}"
[[ -s .run/smoke-module.js ]] || fail 'Grafana served an empty module.js'
echo 'PASS: module.js is served'

phase 6 'Frontend runtime imports'
if rg -q 'react/jsx-runtime' grafana-ai-plugin/dist/module.js .run/smoke-module.js; then
  fail 'module.js still imports react/jsx-runtime'
fi
echo 'PASS: built and served module.js have no react/jsx-runtime import'

phase 7 'Host AI bridge health'
status="$(curl -sS --max-time 10 -o .run/smoke-host-ai.body -w '%{http_code}' \
  http://127.0.0.1:8010/health 2>.run/smoke-host-ai.error || true)"
[[ "$status" == 200 ]] || fail "Host AI bridge returned HTTP ${status:-000}"
rg -q '"ok"[[:space:]]*:[[:space:]]*true' .run/smoke-host-ai.body \
  || fail 'Host AI bridge health body did not report ok=true'
echo 'PASS: host AI bridge is healthy'

phase 8 'Grafana container to host AI bridge network'
if ! docker compose exec -T grafana sh -c \
  'wget -q -O- -T 10 http://host.docker.internal:8010/health' \
  >.run/smoke-container-ai.body 2>.run/smoke-container-ai.error; then
  fail 'Grafana container cannot reach host.docker.internal:8010/health'
fi
rg -q '"ok"[[:space:]]*:[[:space:]]*true' .run/smoke-container-ai.body \
  || fail 'Grafana container reached the AI bridge but got an invalid health body'
echo 'PASS: Grafana container can reach the host AI bridge'

phase 9 'Grafana plugin proxy to AI bridge'
status="$(http_status "$GRAFANA_URL/api/plugin-proxy/$PLUGIN_ID/ai/health" .run/smoke-proxy.body)"
[[ "$status" == 200 ]] || fail "Grafana plugin proxy returned HTTP ${status:-000}"
rg -q '"ok"[[:space:]]*:[[:space:]]*true' .run/smoke-proxy.body \
  || fail 'Grafana plugin proxy returned HTTP 200 with an invalid health body'
echo 'PASS: Grafana plugin proxy reaches the AI bridge'

phase 10 'Grafana app page'
status="$(http_status "$GRAFANA_URL/a/$PLUGIN_ID" .run/smoke-app-page.html)"
[[ "$status" == 200 ]] || fail "Grafana app page returned HTTP ${status:-000}"
rg -qi '<title>Grafana</title>|<title>Grafana[^<]*</title>' .run/smoke-app-page.html \
  || fail 'Grafana app page returned HTTP 200 without the Grafana application shell'
echo 'PASS: app page can be requested'

echo
echo '=== AI PLUGIN SMOKE TEST: PASS ==='
echo "Open: $GRAFANA_URL/a/$PLUGIN_ID"
echo "AI bridge log: $ROOT_DIR/.run/ai-api.log"
