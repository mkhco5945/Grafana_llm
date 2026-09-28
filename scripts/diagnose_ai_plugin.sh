#!/usr/bin/env bash
set -uo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

umask 077
PLUGIN_ID="mkhco-ai-dashboard-app"
GRAFANA_URL="${GRAFANA_URL:-http://127.0.0.1:3000}"
GRAFANA_USER="${GRAFANA_ADMIN_USER:-admin}"
GRAFANA_PASSWORD="${GRAFANA_ADMIN_PASSWORD:-admin}"
STAMP="$(date +%Y%m%d-%H%M%S)"
REPORT_ROOT="$ROOT_DIR/.run/reports"
REPORT_DIR="$REPORT_ROOT/ai-plugin-$STAMP"
if [[ -e "$REPORT_DIR" ]]; then
  REPORT_DIR="$REPORT_DIR-$$"
fi
mkdir -p "$REPORT_DIR"/{build,docker,grafana,network,logs}
REPORT_REL="${REPORT_DIR#"$ROOT_DIR/"}"

redact_stream() {
  sed -E \
    -e 's/([Aa]uthorization:)[[:space:]]*[^[:space:]]+([[:space:]]+[^[:space:]]+)?/\1 [REDACTED]/g' \
    -e 's/([Ss]et-[Cc]ookie:)[[:space:]]*.*/\1 [REDACTED]/g' \
    -e 's/(Bearer)[[:space:]]+[A-Za-z0-9._~+\/-]+/\1 [REDACTED]/g' \
    -e 's#(https?://)[^/@:[:space:]]+:[^/@[:space:]]+@#\1[REDACTED]@#g' \
    -e 's/eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+/[REDACTED_JWT]/g' \
    -e 's/(glsa_|gh[pousr]_)[A-Za-z0-9_-]+/[REDACTED_TOKEN]/g' \
    -e 's/((TOKEN|PASSWORD|SECRET|API_KEY|PRIVATE_KEY)[A-Za-z0-9_]*=)[^[:space:]]+/\1[REDACTED]/Ig' \
    -e 's/("(token|password|secret|apiKey|privateKey)"[[:space:]]*:[[:space:]]*")[^"]*/\1[REDACTED]/Ig'
}

capture() {
  local output="$1"
  shift
  set +e
  "$@" 2>&1 | redact_stream >"$REPORT_DIR/$output"
  local command_rc=${PIPESTATUS[0]}
  return "$command_rc"
}

http_capture() {
  local name="$1" url="$2"
  local raw_headers raw_body curl_error status curl_rc
  raw_headers="$(mktemp)"
  raw_body="$(mktemp)"
  curl_error="$(mktemp)"
  set +e
  status="$(curl -sS --max-time 10 -u "$GRAFANA_USER:$GRAFANA_PASSWORD" \
    -D "$raw_headers" -o "$raw_body" -w '%{http_code}' "$url" 2>"$curl_error")"
  curl_rc=$?
  redact_stream <"$raw_headers" >"$REPORT_DIR/network/$name.headers"
  redact_stream <"$raw_body" >"$REPORT_DIR/network/$name.body"
  redact_stream <"$curl_error" >"$REPORT_DIR/network/$name.error"
  printf 'url=%s\nstatus=%s\ncurl_exit=%s\n' "$url" "${status:-000}" "$curl_rc" \
    >"$REPORT_DIR/network/$name.status"
  rm -f "$raw_headers" "$raw_body" "$curl_error"
  printf '%s' "${status:-000}"
  return "$curl_rc"
}

pass_fail() {
  if [[ "$1" == "1" ]]; then printf 'PASS'; else printf 'FAIL'; fi
}

# Environment: intentionally do not dump the process environment or any .env file.
{
  printf 'timestamp=%s\n' "$(date --iso-8601=seconds 2>/dev/null || date)"
  printf 'working_directory=%s\n' "$ROOT_DIR"
  printf 'git_branch='; git branch --show-current 2>&1 || true
  printf 'git_head='; git rev-parse HEAD 2>&1 || true
  printf '\n[node]\n'; node --version 2>&1 || true
  printf '[npm]\n'; npm --version 2>&1 || true
  printf '[python]\n'; python3 --version 2>&1 || true
  printf '[uv]\n'; uv --version 2>&1 || true
  printf '[docker]\n'; docker --version 2>&1 || true
  printf '[docker compose]\n'; docker compose version 2>&1 || true
} | redact_stream >"$REPORT_DIR/environment.txt"
capture git-status.txt git status --short --branch || true
capture ollama.txt sh -c 'printf "[version]\n"; ollama --version; printf "[running]\n"; ollama ps; printf "[models]\n"; ollama list; printf "[api]\n"; curl -sS --max-time 5 http://127.0.0.1:11434/api/tags' || true

# Build evidence.
set +e
(cd grafana-ai-plugin && npm run typecheck) >"$REPORT_DIR/build/typecheck.txt" 2>&1
TYPECHECK_RC=$?
(cd grafana-ai-plugin && npm run build) >"$REPORT_DIR/build/webpack.txt" 2>&1
BUILD_RC=$?
printf '%s\n' "$TYPECHECK_RC" >"$REPORT_DIR/build/typecheck.exit"
printf '%s\n' "$BUILD_RC" >"$REPORT_DIR/build/webpack.exit"
capture build/dist-files.txt find grafana-ai-plugin/dist -maxdepth 3 -type f -printf '%M %u:%g %s %TY-%Tm-%TdT%TH:%TM:%TS %p\n' || true
capture build/source-plugin.json sed -n '1,260p' grafana-ai-plugin/src/plugin.json || true
capture build/dist-plugin.json sed -n '1,260p' grafana-ai-plugin/dist/plugin.json || true
capture build/plugin-json-checksums.txt sha256sum grafana-ai-plugin/src/plugin.json grafana-ai-plugin/dist/plugin.json || true
set +e
diff -u grafana-ai-plugin/src/plugin.json grafana-ai-plugin/dist/plugin.json >"$REPORT_DIR/build/plugin-json.diff" 2>&1
PLUGIN_JSON_DIFF_RC=$?
if [[ -f grafana-ai-plugin/dist/module.js ]]; then
  if rg -n 'react/jsx-runtime' grafana-ai-plugin/dist/module.js >"$REPORT_DIR/build/react-jsx-runtime.txt" 2>&1; then
    JSX_RUNTIME_PRESENT=1
  else
    JSX_RUNTIME_PRESENT=0
    printf 'PASS: react/jsx-runtime is absent from dist/module.js\n' >"$REPORT_DIR/build/react-jsx-runtime.txt"
  fi
else
  JSX_RUNTIME_PRESENT=1
  printf 'FAIL: dist/module.js does not exist\n' >"$REPORT_DIR/build/react-jsx-runtime.txt"
fi

# Docker state without Config.Env (which can contain service tokens).
capture docker/compose-ps.txt docker compose ps || true
capture docker/compose-ps-all.txt docker compose ps -a || true
capture docker/container-state.txt sh -c '
  for service in prometheus demo grafana mcp-grafana; do
    id=$(docker compose ps -q "$service" 2>/dev/null)
    if [ -n "$id" ]; then
      docker inspect --format "service=$service id={{.Id}} name={{.Name}} image={{.Config.Image}} status={{.State.Status}} health={{if .State.Health}}{{.State.Health.Status}}{{else}}n/a{{end}} started={{.State.StartedAt}}" "$id"
    else
      echo "service=$service id=missing status=missing health=n/a"
    fi
  done
' || true
capture docker/grafana-mounts.txt sh -c '
  id=$(docker compose ps -q grafana 2>/dev/null)
  [ -n "$id" ] || { echo "Grafana container is not present"; exit 1; }
  docker inspect --format "{{range .Mounts}}{{.Type}} {{.Source}} -> {{.Destination}} mode={{.Mode}} rw={{.RW}}{{println}}{{end}}" "$id"
' || true
capture docker/grafana-version.txt docker compose exec -T grafana grafana cli --version || true
capture docker/plugin-files.txt docker compose exec -T grafana sh -c 'find /var/lib/grafana/plugins/mkhco-ai-dashboard-app -maxdepth 3 -type f -exec ls -ln {} \;' || true
capture docker/container-plugin.json docker compose exec -T grafana sh -c 'cat /var/lib/grafana/plugins/mkhco-ai-dashboard-app/plugin.json' || true
capture docker/container-plugin-checksums.txt docker compose exec -T grafana sh -c 'sha256sum /var/lib/grafana/plugins/mkhco-ai-dashboard-app/plugin.json /var/lib/grafana/plugins/mkhco-ai-dashboard-app/module.js' || true
capture docker/container-provisioning.txt docker compose exec -T grafana sh -c 'find /etc/grafana/provisioning -maxdepth 3 -type f -print; printf "\n[plugin provisioning]\n"; sed -n "1,200p" /etc/grafana/provisioning/plugins/ai-dashboard.yaml 2>&1' || true

# Grafana API state. Response bodies are saved separately from exact statuses.
GRAFANA_HEALTH_STATUS="$(http_capture grafana-health "$GRAFANA_URL/api/health" || true)"
PLUGINS_STATUS="$(http_capture grafana-plugins "$GRAFANA_URL/api/plugins" || true)"
SETTINGS_STATUS="$(http_capture plugin-settings "$GRAFANA_URL/api/plugins/$PLUGIN_ID/settings" || true)"
MODULE_STATUS="$(http_capture plugin-module "$GRAFANA_URL/public/plugins/$PLUGIN_ID/module.js" || true)"
APP_STATUS="$(http_capture app-page "$GRAFANA_URL/a/$PLUGIN_ID" || true)"
PROXY_STATUS="$(http_capture plugin-proxy "$GRAFANA_URL/api/plugin-proxy/$PLUGIN_ID/ai/health" || true)"

# Host bridge request has no credentials.
set +e
HOST_AI_STATUS="$(curl -sS --max-time 10 -D "$REPORT_DIR/network/host-ai.headers" \
  -o "$REPORT_DIR/network/host-ai.body" -w '%{http_code}' http://127.0.0.1:8010/health \
  2>"$REPORT_DIR/network/host-ai.error")"
HOST_AI_RC=$?
printf 'url=http://127.0.0.1:8010/health\nstatus=%s\ncurl_exit=%s\n' \
  "${HOST_AI_STATUS:-000}" "$HOST_AI_RC" >"$REPORT_DIR/network/host-ai.status"

set +e
docker compose exec -T grafana sh -c \
  'wget -S -O- -T 10 http://host.docker.internal:8010/health' \
  >"$REPORT_DIR/network/grafana-container-to-ai.txt" 2>&1
CONTAINER_AI_RC=$?
redact_stream <"$REPORT_DIR/network/grafana-container-to-ai.txt" >"$REPORT_DIR/network/grafana-container-to-ai.redacted"
mv "$REPORT_DIR/network/grafana-container-to-ai.redacted" "$REPORT_DIR/network/grafana-container-to-ai.txt"

# Logs are tailed and redacted. Never collect .env or an unfiltered environment dump.
set +e
docker compose logs --no-color --tail=1200 grafana 2>&1 \
  | rg -i "$PLUGIN_ID|plugin-proxy|plugin proxy|plugin setting|provision|8010|status=(404|500|502|503)" \
  | redact_stream >"$REPORT_DIR/logs/grafana-filtered.log"
for service in grafana mcp-grafana prometheus demo; do
  docker compose logs --no-color --tail=250 "$service" 2>&1 \
    | redact_stream >"$REPORT_DIR/logs/docker-$service.log" || true
done
if [[ -f .run/ai-api.log ]]; then
  tail -n 500 .run/ai-api.log | redact_stream >"$REPORT_DIR/logs/ai-api.log"
else
  printf '.run/ai-api.log does not exist\n' >"$REPORT_DIR/logs/ai-api.log"
fi

# Evaluate layers using only evidence collected above.
BUILD_OK=0; [[ "$TYPECHECK_RC" == 0 && "$BUILD_RC" == 0 ]] && BUILD_OK=1
JSON_OK=0; [[ "$PLUGIN_JSON_DIFF_RC" == 0 ]] && JSON_OK=1
JSX_OK=0; [[ "$JSX_RUNTIME_PRESENT" == 0 ]] && JSX_OK=1
GRAFANA_OK=0; [[ "$GRAFANA_HEALTH_STATUS" == 200 ]] && GRAFANA_OK=1
PLUGIN_REGISTERED=0
[[ "$PLUGINS_STATUS" == 200 ]] && rg -q "\"id\":\"$PLUGIN_ID\"" "$REPORT_DIR/network/grafana-plugins.body" && PLUGIN_REGISTERED=1
SETTINGS_ENABLED=0
[[ "$SETTINGS_STATUS" == 200 ]] && rg -q '"enabled"[[:space:]]*:[[:space:]]*true' "$REPORT_DIR/network/plugin-settings.body" && SETTINGS_ENABLED=1
MODULE_OK=0; [[ "$MODULE_STATUS" == 200 ]] && MODULE_OK=1
HOST_AI_OK=0; [[ "$HOST_AI_STATUS" == 200 && "$HOST_AI_RC" == 0 ]] && HOST_AI_OK=1
CONTAINER_AI_OK=0; [[ "$CONTAINER_AI_RC" == 0 ]] && rg -q '200 OK' "$REPORT_DIR/network/grafana-container-to-ai.txt" && CONTAINER_AI_OK=1
PROXY_OK=0; [[ "$PROXY_STATUS" == 200 ]] && PROXY_OK=1
APP_OK=0; [[ "$APP_STATUS" == 200 ]] && APP_OK=1

FIRST_FAILURE="none"
LIKELY_CAUSE="All observed smoke-test layers passed."
if [[ "$BUILD_OK" == 0 ]]; then
  FIRST_FAILURE="plugin build"
  LIKELY_CAUSE="TypeScript typecheck or webpack build failed; inspect build/typecheck.txt and build/webpack.txt."
elif [[ "$JSON_OK" == 0 ]]; then
  FIRST_FAILURE="built metadata"
  LIKELY_CAUSE="src/plugin.json and dist/plugin.json differ, so Grafana may be reading stale metadata."
elif [[ "$JSX_OK" == 0 ]]; then
  FIRST_FAILURE="frontend bundle imports"
  LIKELY_CAUSE="dist/module.js is missing or still imports react/jsx-runtime."
elif [[ "$GRAFANA_OK" == 0 ]]; then
  FIRST_FAILURE="Grafana health"
  LIKELY_CAUSE="Grafana is unavailable; use the container state and Grafana logs."
elif [[ "$PLUGIN_REGISTERED" == 0 ]]; then
  FIRST_FAILURE="plugin registration"
  LIKELY_CAUSE="Grafana did not register $PLUGIN_ID; compare the mounted plugin files with registration logs."
elif [[ "$SETTINGS_ENABLED" == 0 ]]; then
  FIRST_FAILURE="app settings"
  LIKELY_CAUSE="The org-scoped app setting is missing or disabled."
elif [[ "$MODULE_OK" == 0 ]]; then
  FIRST_FAILURE="module serving"
  LIKELY_CAUSE="Grafana registered the app but did not serve its module.js."
elif [[ "$HOST_AI_OK" == 0 ]]; then
  FIRST_FAILURE="host AI bridge"
  LIKELY_CAUSE="The host AI bridge is not healthy on 127.0.0.1:8010."
elif [[ "$CONTAINER_AI_OK" == 0 ]]; then
  FIRST_FAILURE="Grafana-to-host network"
  LIKELY_CAUSE="The Grafana container cannot reach host.docker.internal:8010."
elif [[ "$PROXY_OK" == 0 ]]; then
  FIRST_FAILURE="Grafana plugin proxy"
  if [[ "$PROXY_STATUS" == 404 ]] && rg -q 'plugin route match not found' "$REPORT_DIR/network/plugin-proxy.body"; then
    LIKELY_CAUSE="Grafana found the plugin settings but no loaded plugin.json route matched ai/health. Verify path ai/* and restart Grafana after rebuilding metadata."
  elif [[ "$PROXY_STATUS" == 500 ]] && rg -qi 'plugin setting not found' "$REPORT_DIR/network/plugin-proxy.body"; then
    LIKELY_CAUSE="The org-scoped plugin settings row is missing."
  elif [[ "$PROXY_STATUS" == 502 || "$PROXY_STATUS" == 503 ]]; then
    LIKELY_CAUSE="Grafana matched the proxy route but could not obtain a successful upstream response."
  else
    LIKELY_CAUSE="The proxy failed after its prerequisites passed; inspect the exact response and filtered Grafana logs."
  fi
elif [[ "$APP_OK" == 0 ]]; then
  FIRST_FAILURE="app page"
  LIKELY_CAUSE="The app backend layers passed but Grafana did not return the app page."
fi

cat >"$REPORT_DIR/summary.md" <<EOF
# Grafana AI plugin diagnostic summary

- Generated: $(date --iso-8601=seconds 2>/dev/null || date)
- Plugin: \`$PLUGIN_ID\`
- First failing layer: **$FIRST_FAILURE**
- Evidence-based likely cause: $LIKELY_CAUSE

| Layer | Result | Evidence |
|---|---|---|
| Typecheck + webpack | $(pass_fail "$BUILD_OK") | \`build/typecheck.txt\`, \`build/webpack.txt\` |
| Source/dist plugin.json identical | $(pass_fail "$JSON_OK") | \`build/plugin-json.diff\`, \`build/plugin-json-checksums.txt\` |
| No react/jsx-runtime import | $(pass_fail "$JSX_OK") | \`build/react-jsx-runtime.txt\` |
| Grafana health (HTTP $GRAFANA_HEALTH_STATUS) | $(pass_fail "$GRAFANA_OK") | \`network/grafana-health.*\` |
| Plugin registered (HTTP $PLUGINS_STATUS) | $(pass_fail "$PLUGIN_REGISTERED") | \`network/grafana-plugins.*\` |
| App setting enabled (HTTP $SETTINGS_STATUS) | $(pass_fail "$SETTINGS_ENABLED") | \`network/plugin-settings.*\` |
| module.js served (HTTP $MODULE_STATUS) | $(pass_fail "$MODULE_OK") | \`network/plugin-module.*\` |
| Host AI bridge (HTTP ${HOST_AI_STATUS:-000}) | $(pass_fail "$HOST_AI_OK") | \`network/host-ai.*\` |
| Grafana container to AI bridge | $(pass_fail "$CONTAINER_AI_OK") | \`network/grafana-container-to-ai.txt\` |
| Plugin proxy (HTTP $PROXY_STATUS) | $(pass_fail "$PROXY_OK") | \`network/plugin-proxy.*\` |
| App page (HTTP $APP_STATUS) | $(pass_fail "$APP_OK") | \`network/app-page.*\` |

## Important raw evidence

- Runtime/plugin metadata: \`docker/container-plugin.json\`, \`docker/container-plugin-checksums.txt\`
- Runtime mounts and state: \`docker/grafana-mounts.txt\`, \`docker/container-state.txt\`
- Grafana API responses: \`network/grafana-plugins.body\`, \`network/plugin-settings.body\`
- Exact proxy headers/body/status: \`network/plugin-proxy.headers\`, \`network/plugin-proxy.body\`, \`network/plugin-proxy.status\`
- Filtered logs: \`logs/grafana-filtered.log\`, \`logs/ai-api.log\`

Secrets are intentionally not collected. Captured output is redacted for authorization headers, cookies, bearer tokens, and common token/password/secret fields.
EOF

ln -sfn "$(basename "$REPORT_DIR")" "$REPORT_ROOT/latest-ai-plugin"
printf '%s\n' "$REPORT_REL/summary.md"
