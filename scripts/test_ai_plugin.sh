#!/usr/bin/env bash
set -uo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

PLUGIN_ID="mkhco-ai-dashboard-app"
GRAFANA_URL="${GRAFANA_URL:-http://127.0.0.1:3000}"
GRAFANA_USER="${GRAFANA_ADMIN_USER:-admin}"
GRAFANA_PASSWORD="${GRAFANA_ADMIN_PASSWORD:-admin}"
PROXY_BASE="$GRAFANA_URL/api/plugin-proxy/$PLUGIN_ID/ai"
SMOKE_SESSION_ID=""
SECOND_SESSION_ID=""
mkdir -p .run

cleanup_smoke_sessions() {
  local session_id
  for session_id in "$SMOKE_SESSION_ID" "$SECOND_SESSION_ID"; do
    [[ -n "$session_id" ]] || continue
    curl -sS --max-time 5 -X DELETE "http://127.0.0.1:8010/sessions/$session_id" >/dev/null 2>&1 || true
  done
}

fail() {
  local reason="$1" report_path
  printf 'FAIL: %s\n' "$reason" >&2
  report_path="$(bash scripts/diagnose_ai_plugin.sh)" || true
  cleanup_smoke_sessions
  if [[ -n "$report_path" ]]; then
    printf 'RCA report: %s\n' "$report_path" >&2
  else
    printf 'RCA report: diagnostic collector failed before returning a path\n' >&2
  fi
  exit 1
}

http_get() {
  local url="$1" output="$2"
  curl -sS --max-time 15 -u "$GRAFANA_USER:$GRAFANA_PASSWORD" \
    -o "$output" -w '%{http_code}' "$url" 2>"$output.error" || true
}

http_post() {
  local url="$1" data="$2" output="$3"
  curl -sS --max-time 15 -u "$GRAFANA_USER:$GRAFANA_PASSWORD" \
    -H 'Content-Type: application/json' -X POST -d "$data" \
    -o "$output" -w '%{http_code}' "$url" 2>"$output.error" || true
}

json_value() {
  local file="$1" expression="$2"
  python3 -c "import json,sys; data=json.load(open(sys.argv[1])); print($expression)" "$file"
}

phase() { printf '\n[%s/14] %s\n' "$1" "$2"; }

phase 1 'Plugin typecheck and build'
if [[ ! -x grafana-ai-plugin/node_modules/.bin/webpack || ! -x grafana-ai-plugin/node_modules/.bin/tsc ]]; then
  (cd grafana-ai-plugin && npm ci --no-audit --no-fund) \
    >.run/smoke-npm-install.log 2>&1 || fail 'Grafana plugin dependency installation failed'
fi
(cd grafana-ai-plugin && npm run typecheck) || fail 'Grafana plugin typecheck failed'
(cd grafana-ai-plugin && npm run build) || fail 'Grafana plugin webpack build failed'
[[ -s grafana-ai-plugin/dist/module.js ]] || fail 'Webpack did not produce dist/module.js'
if rg -q 'react/jsx-runtime' grafana-ai-plugin/dist/module.js; then
  fail 'Built module.js still imports react/jsx-runtime'
fi
echo 'PASS: plugin typecheck/build and runtime imports'

phase 2 'Start stack and verify Grafana health'
AI_PLUGIN_ALREADY_BUILT=1 AI_PLUGIN_SKIP_PROXY_CHECK=1 ./start.sh \
  || fail 'Local stack or host AI bridge startup failed'
status="$(http_get "$GRAFANA_URL/api/health" .run/smoke-grafana-health.body)"
[[ "$status" == 200 ]] || fail "Grafana health returned HTTP ${status:-000}"
echo 'PASS: Grafana is healthy'

phase 3 'Plugin registration and enabled setting'
status="$(http_get "$GRAFANA_URL/api/plugins" .run/smoke-plugins.body)"
[[ "$status" == 200 ]] || fail "Grafana plugin list returned HTTP ${status:-000}"
rg -q "\"id\":\"$PLUGIN_ID\"" .run/smoke-plugins.body || fail "Grafana did not register $PLUGIN_ID"
status="$(http_get "$GRAFANA_URL/api/plugins/$PLUGIN_ID/settings" .run/smoke-settings.body)"
[[ "$status" == 200 ]] || fail "Grafana plugin settings returned HTTP ${status:-000}"
rg -q '"enabled"[[:space:]]*:[[:space:]]*true' .run/smoke-settings.body \
  || fail 'Grafana AI app setting exists but is not enabled'
rg -q '"module"[[:space:]]*:[[:space:]]*"public/plugins/mkhco-ai-dashboard-app/module.js"' .run/smoke-settings.body \
  || fail 'Grafana plugin metadata points to an unexpected module'
echo 'PASS: plugin is registered, enabled, and points to module.js'

phase 4 'Built, mounted, and served plugin files are current'
cmp -s grafana-ai-plugin/src/plugin.json grafana-ai-plugin/dist/plugin.json \
  || fail 'Built plugin.json differs from source plugin.json'
docker compose exec -T grafana cat /var/lib/grafana/plugins/$PLUGIN_ID/plugin.json >.run/smoke-container-plugin.json \
  || fail 'Could not read plugin.json inside Grafana'
cmp -s grafana-ai-plugin/dist/plugin.json .run/smoke-container-plugin.json \
  || fail 'Grafana container plugin.json differs from the fresh build'
docker compose exec -T grafana cat /var/lib/grafana/plugins/$PLUGIN_ID/module.js >.run/smoke-container-module.js \
  || fail 'Could not read module.js inside Grafana'
cmp -s grafana-ai-plugin/dist/module.js .run/smoke-container-module.js \
  || fail 'Grafana container module.js differs from the fresh build'
status="$(http_get "$GRAFANA_URL/public/plugins/$PLUGIN_ID/module.js" .run/smoke-served-module.js)"
[[ "$status" == 200 ]] || fail "Grafana module.js request returned HTTP ${status:-000}"
cmp -s grafana-ai-plugin/dist/module.js .run/smoke-served-module.js \
  || fail 'module.js served by Grafana differs from the fresh build'
echo 'PASS: source metadata and built/mounted/served plugin bytes match'

phase 5 'Host AI bridge and SQLite persistence health'
status="$(curl -sS --max-time 10 -o .run/smoke-host-ai.body -w '%{http_code}' \
  http://127.0.0.1:8010/health 2>.run/smoke-host-ai.error || true)"
[[ "$status" == 200 ]] || fail "Host AI bridge returned HTTP ${status:-000}"
rg -q '"database"[[:space:]]*:[[:space:]]*"ok"' .run/smoke-host-ai.body \
  || fail 'AI bridge health did not report database=ok'
[[ -s .state/ai-chat.sqlite3 ]] || fail 'Persistent chat database .state/ai-chat.sqlite3 does not exist'
status="$(curl -sS --max-time 10 -o .run/smoke-stats.body -w '%{http_code}' http://127.0.0.1:8010/stats || true)"
[[ "$status" == 200 ]] || fail "AI bridge persistence stats returned HTTP ${status:-000}"
echo 'PASS: host AI bridge and SQLite database are healthy'

phase 6 'Grafana container to host AI bridge network'
if ! docker compose exec -T grafana sh -c \
  'wget -q -O- -T 10 http://host.docker.internal:8010/health' \
  >.run/smoke-container-ai.body 2>.run/smoke-container-ai.error; then
  fail 'Grafana container cannot reach host.docker.internal:8010/health'
fi
rg -q '"ok"[[:space:]]*:[[:space:]]*true' .run/smoke-container-ai.body \
  || fail 'Grafana container reached the AI bridge but got an invalid health body'
echo 'PASS: Grafana container can reach the host AI bridge'

phase 7 'Grafana plugin proxy'
status="$(http_get "$PROXY_BASE/health" .run/smoke-proxy.body)"
[[ "$status" == 200 ]] || fail "Grafana plugin proxy returned HTTP ${status:-000}"
status="$(http_get "$PROXY_BASE/stats" .run/smoke-proxy-stats.body)"
[[ "$status" == 200 ]] || fail "Grafana plugin proxy persistence API returned HTTP ${status:-000}"
echo 'PASS: Grafana plugin proxy reaches health and persistence APIs'

phase 8 'Create and list a persistent chat session'
smoke_title="[smoke-test] Persistence $(date +%Y%m%d-%H%M%S)"
status="$(http_post "$PROXY_BASE/sessions" "{\"title\":\"$smoke_title\"}" .run/smoke-create-session.body)"
[[ "$status" == 201 ]] || fail "Create-session API returned HTTP ${status:-000}"
SMOKE_SESSION_ID="$(json_value .run/smoke-create-session.body 'data["id"]')" || fail 'Create-session response was invalid JSON'
[[ "$SMOKE_SESSION_ID" =~ ^[a-f0-9]{32}$ ]] || fail 'Create-session response had an invalid session ID'
status="$(http_get "$PROXY_BASE/sessions" .run/smoke-list-sessions.body)"
[[ "$status" == 200 ]] || fail "List-sessions API returned HTTP ${status:-000}"
python3 -c 'import json,sys; data=json.load(open(sys.argv[1])); sid=sys.argv[2]; raise SystemExit(0 if any(s["id"]==sid for s in data["sessions"]) else 1)' \
  .run/smoke-list-sessions.body "$SMOKE_SESSION_ID" || fail 'New session was absent from the session list'
echo "PASS: persistent session created and listed ($SMOKE_SESSION_ID)"

phase 9 'Complete a lightweight model-backed persisted chat job'
status="$(http_post "$PROXY_BASE/sessions/$SMOKE_SESSION_ID/chat" \
  '{"message":"Reply briefly that the local Grafana AI integration is responding. Do not use tools."}' .run/smoke-start-chat.body)"
[[ "$status" == 202 ]] || fail "Session chat API returned HTTP ${status:-000}"
SMOKE_JOB_ID="$(json_value .run/smoke-start-chat.body 'data["job_id"]')" || fail 'Start-chat response was invalid JSON'
job_deadline=$((SECONDS + 420))
while :; do
  status="$(http_get "$PROXY_BASE/jobs/$SMOKE_JOB_ID" .run/smoke-job.body)"
  [[ "$status" == 200 ]] || fail "Job API returned HTTP ${status:-000}"
  job_status="$(json_value .run/smoke-job.body 'data["status"]')"
  [[ "$job_status" == completed ]] && break
  [[ "$job_status" != failed && "$job_status" != interrupted ]] || fail "Persistence smoke job ended as $job_status"
  (( SECONDS < job_deadline )) || fail 'Model-backed persistence smoke job did not finish in 420 seconds'
  sleep 0.25
done
status="$(http_get "$PROXY_BASE/sessions/$SMOKE_SESSION_ID" .run/smoke-session-completed.body)"
[[ "$status" == 200 ]] || fail "Reload-session API returned HTTP ${status:-000}"
python3 -c '
import json, sys
data=json.load(open(sys.argv[1]))
roles=[item["role"] for item in data["messages"]]
assistant=[item for item in data["messages"] if item["role"]=="assistant" and item.get("job_id")==sys.argv[2]]
raise SystemExit(0 if roles.count("user")==1 and len(assistant)==1 and data["jobs"][-1]["status"]=="completed" else 1)
' .run/smoke-session-completed.body "$SMOKE_JOB_ID" || fail 'Completed user/assistant records were not persisted exactly once'
echo 'PASS: real model-backed user message, progress, completed job, and one assistant response persisted'

phase 10 'Render the real Grafana app and restored conversation in Chrome'
if ! AI_PLUGIN_REQUIRE_ASSISTANT=1 \
  node scripts/check_ai_plugin_frontend.mjs .run/smoke-frontend.json >.run/smoke-frontend.stdout 2>.run/smoke-frontend.stderr; then
  fail 'Grafana frontend did not render the AI root/restored conversation or raised a browser exception'
fi
rg -q '"markerVisible"[[:space:]]*:[[:space:]]*true' .run/smoke-frontend.json \
  || fail 'Rendered Grafana page is missing data-testid=ai-dashboard-builder-root'
rg -q '"appNotFoundVisible"[[:space:]]*:[[:space:]]*false' .run/smoke-frontend.json \
  || fail 'Rendered Grafana page shows App not found'
rg -q 'module.js\?_cache=0.3.0' .run/smoke-frontend.json \
  || fail 'Browser did not load the cache-busted 0.3.0 plugin module'
rg -q '"assistantMessageCount"[[:space:]]*:[[:space:]]*[1-9]' .run/smoke-frontend.json \
  || fail 'Rendered Grafana page did not restore the persisted assistant response'
echo 'PASS: real browser rendered the root marker and server-restored assistant response without App not found'

phase 11 'Safely restart the AI bridge'
status="$(curl -sS --max-time 10 -o .run/smoke-restart-stats.body -w '%{http_code}' http://127.0.0.1:8010/stats || true)"
[[ "$status" == 200 ]] || fail "Could not check active jobs before AI bridge restart (HTTP ${status:-000})"
active_jobs="$(python3 -c 'import json; data=json.load(open(".run/smoke-restart-stats.body")); print(sum(data.get("jobs_by_status",{}).get(k,0) for k in ("queued","running")))')"
[[ "$active_jobs" == 0 ]] || fail "Refusing to restart AI bridge while $active_jobs job(s) are active"
AI_BRIDGE_FORCE_RESTART=1 AI_PLUGIN_ALREADY_BUILT=1 AI_PLUGIN_SKIP_PROXY_CHECK=1 ./start.sh \
  || fail 'AI bridge restart failed during persistence verification'
echo 'PASS: AI bridge restarted without deleting the chat database'

phase 12 'Reload the completed conversation after bridge restart'
status="$(http_get "$PROXY_BASE/sessions/$SMOKE_SESSION_ID" .run/smoke-session-after-restart.body)"
[[ "$status" == 200 ]] || fail "Persisted session returned HTTP ${status:-000} after bridge restart"
python3 -c '
import json, sys
data=json.load(open(sys.argv[1]))
assistant=[item for item in data["messages"] if item["role"]=="assistant" and item.get("job_id")==sys.argv[2]]
raise SystemExit(0 if len(assistant)==1 and data["jobs"][-1]["status"]=="completed" else 1)
' .run/smoke-session-after-restart.body "$SMOKE_JOB_ID" || fail 'Completed conversation changed or disappeared after AI bridge restart'
echo 'PASS: same completed conversation is readable after bridge restart'

phase 13 'New chat keeps previous conversations'
status="$(http_post "$PROXY_BASE/sessions" '{"title":"[smoke-test] New-chat isolation"}' .run/smoke-second-session.body)"
[[ "$status" == 201 ]] || fail "Second create-session API returned HTTP ${status:-000}"
SECOND_SESSION_ID="$(json_value .run/smoke-second-session.body 'data["id"]')"
status="$(http_get "$PROXY_BASE/sessions" .run/smoke-list-after-second.body)"
[[ "$status" == 200 ]] || fail "List-sessions API returned HTTP ${status:-000} after New chat"
python3 -c '
import json, sys
ids={item["id"] for item in json.load(open(sys.argv[1]))["sessions"]}
raise SystemExit(0 if {sys.argv[2],sys.argv[3]} <= ids else 1)
' .run/smoke-list-after-second.body "$SMOKE_SESSION_ID" "$SECOND_SESSION_ID" \
  || fail 'Creating a new chat removed the prior conversation'
echo 'PASS: New chat creates another session without deleting history'

phase 14 'Clean isolated smoke data and finalize'
cleanup_smoke_sessions
SMOKE_SESSION_ID=""
SECOND_SESSION_ID=""
status="$(http_get "$GRAFANA_URL/a/$PLUGIN_ID" .run/smoke-app-shell.body)"
[[ "$status" == 200 ]] || fail "Grafana app shell returned HTTP ${status:-000}"
echo 'PASS: smoke sessions cleaned; real chats were untouched'

echo
echo '=== AI PLUGIN SMOKE TEST: PASS ==='
echo "Open: $GRAFANA_URL/a/$PLUGIN_ID"
echo 'Persistent chats: .state/ai-chat.sqlite3'
echo "AI bridge log: $ROOT_DIR/.run/ai-api.log"
