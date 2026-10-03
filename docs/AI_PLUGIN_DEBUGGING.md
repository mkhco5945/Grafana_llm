# Grafana AI plugin debugging and persistence

## Architecture

```text
Grafana app (/a/mkhco-ai-dashboard-app)
  -> Grafana plugin proxy (/api/plugin-proxy/mkhco-ai-dashboard-app/ai/*)
  -> ai-bridge container (ai-bridge:8010)
  -> SQLite (.state/ai-chat.sqlite3)
  -> Ollama or OpenAI-compatible API + mcp-grafana -> Grafana/Prometheus
```

The browser never calls port 8010 directly. Grafana 12.1.1 loads the app with `AppPlugin.setRootPage` and forwards the `ai/*` proxy route to the host bridge. `start.sh` rebuilds the plugin and restarts Grafana because plugin metadata is read at Grafana startup.

## Why “App not found” happened

Grafana had valid plugin settings and the current `module.js` bytes execute successfully in Grafana 12.1.1. The failure was frontend artifact invalidation: all earlier bundles—including the broken automatic-JSX bundle that requested `/react/jsx-runtime`—advertised plugin version `0.1.0`. Grafana therefore requested every rewrite as `module.js?_cache=0.1.0`, and serves plugin assets with a one-hour public cache. A browser with a cached/rejected module import could keep rendering Grafana’s `App not found` fallback even while curl saw the new file and every backend check passed.

The plugin version is now `0.2.0`, which changes Grafana’s module cache key. The bundle still uses the classic React transform, extension links use the Grafana-required `/a/<pluginId>/` form, and the page exposes `data-testid="ai-dashboard-builder-root"`. The headless Chrome check requires that marker, a successful `module.js?_cache=0.2.0` response, no `App not found`/`Page not found`, and no browser JavaScript exception.

Relevant Grafana 12.1.1 source:

- [App root loading and “App not found” fallback](https://github.com/grafana/grafana/blob/v12.1.1/public/app/features/plugins/components/AppRootPage.tsx)
- [Plugin module version cache key](https://github.com/grafana/grafana/blob/v12.1.1/public/app/features/plugins/loader/cache.ts)
- [`AppPlugin.setRootPage`](https://github.com/grafana/grafana/blob/v12.1.1/packages/grafana-data/src/types/app.ts)
- [App proxy routes](https://grafana.com/developers/plugin-tools/how-to-guides/app-plugins/add-authentication-for-app-plugins)

## Persistent conversations

For provider selection and key handling see [MODEL_PROVIDERS.md](MODEL_PROVIDERS.md).
The current plugin version is `0.5.0`; the `0.2.0` discussion above records the
original cache fix. Current smoke checks expect `module.js?_cache=0.5.0`.
`GET /connection` exposes defaults and a boolean key-presence indicator, never the
key. Chat and retry requests accept a `connection` object with provider, base URL,
model and optional API key. Jobs persist only the non-secret fields.

SQLite is the source of truth for sessions, messages, jobs, progress, results, errors, model names, and dashboard URLs. The frontend only uses localStorage for unsent drafts, the last selected session, and one-time migration bookkeeping.

- `GET /sessions` lists chat history.
- `POST /sessions` creates a new chat without deleting older chats.
- `GET /sessions/{id}` restores messages and jobs.
- `POST /sessions/{id}/chat` stores the user message and starts a durable job record.
- `GET /jobs/{id}` reports persisted progress/results.
- `POST /jobs/{id}/retry` retries a failed or interrupted saved request.

Leaving the Grafana page does not stop the Python worker. Returning to the page reloads the session and resumes polling its active job. Completed answers are inserted with a unique job ID, preventing duplicate assistant messages.

If the AI bridge container restarts, startup converts stale `queued`/`running` jobs to `interrupted`. The original request and prior messages remain, and the UI offers **Retry**. Completed conversations survive browser, Grafana, and bridge restarts. Compose uses `restart: unless-stopped`, and `.state/ai-chat.sqlite3` is mounted from the project directory.

On first successful load, the frontend looks for `mkhco-ai-dashboard-app.chat.v1`, imports useful messages into one SQLite session, and marks migration complete. It does not delete or overwrite the old localStorage value. Browser localStorage cannot be recovered server-side if that browser no longer has it.

## Validation and reports

Run the complete smoke test:

```bash
bash scripts/test_ai_plugin.sh
```

It verifies builds and checksums, registration/settings/proxy layers, SQLite APIs, a completed isolated conversation through the real local model/MCP worker, a safe AI-bridge restart, post-restart retrieval, New chat isolation, and the actual rendered app in headless Chrome. Test sessions use `[smoke-test]` titles and are the only sessions the cleanup endpoint permits deleting.

Collect a mostly read-only report:

```bash
bash scripts/diagnose_ai_plugin.sh
```

Reports live under `.run/reports/ai-plugin-YYYYMMDD-HHMMSS/`, with `.run/reports/latest-ai-plugin` pointing to the newest report. `summary.md` identifies the first failed layer. Notable evidence:

- `build/`: typecheck/build output; metadata and module checksums; JSX-runtime check.
- `docker/`: container state, mounts, Grafana version, and plugin files visible inside Grafana.
- `frontend/render.json`: root marker, not-found signals, module response/cache status, and browser exceptions.
- `network/`: exact status/headers/body for health, settings, module, proxy, and stats requests.
- `state/sqlite-summary.json`: database existence, counts by status, and latest IDs/timestamps—never messages.
- `logs/`: redacted Grafana/bridge errors.

The collector does not read `.env`, dump environments, or include chat messages/prompts. It redacts common authorization, cookie, password, and token formats.

## Start, stop, and intentional deletion

`start.sh` never erases `.state/ai-chat.sqlite3`. It builds and starts the `ai-bridge` service with the rest of the Compose stack. `stop.sh` stops the containers but preserves chat history.

To intentionally erase all chat history, first run `./stop.sh`, then move `.state/ai-chat.sqlite3` and its `-wal`/`-shm` companions to a backup location or delete those three files explicitly. This is deliberately not part of either normal script.

Never share `.env`, Grafana service-account tokens, `MCP_GRAFANA_SERVER_TOKEN`, `GRAFANA_MCP_SERVICE_ACCOUNT_TOKEN`, cookies, private keys, or raw environment dumps. `.state/` and `.run/` are git-ignored.
