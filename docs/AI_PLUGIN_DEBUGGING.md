# Grafana AI plugin debugging

## Request flow

```text
browser
  -> Grafana /api/plugin-proxy/mkhco-ai-dashboard-app/ai/*
  -> plugin.json route "ai/*"
  -> http://host.docker.internal:8010/*
  -> host agent.api
  -> Ollama + mcp-grafana
  -> Grafana/Prometheus
```

The browser never calls port 8010 directly. Grafana 12.1.1 supports data-proxy routes for app plugins. Its route matcher treats `ai` as an exact path and `ai/*` as the prefix route that forwards `/health`, `/chat`, and `/jobs/...`. Grafana reads `plugin.json` into memory at startup, so `start.sh` restarts Grafana after a plugin build.

Relevant upstream references:

- [Grafana app-plugin proxy route guide](https://grafana.com/developers/plugin-tools/how-to-guides/app-plugins/add-authentication-for-app-plugins)
- [Grafana 12.1.1 proxy matcher](https://github.com/grafana/grafana/blob/v12.1.1/pkg/api/pluginproxy/pluginproxy.go#L59-L98)
- [Grafana 12.1.1 wildcard route tests](https://github.com/grafana/grafana/blob/v12.1.1/pkg/api/pluginproxy/pluginproxy_test.go#L284-L444)

## One-command checks

Run the complete, ordered smoke test:

```bash
bash scripts/test_ai_plugin.sh
```

It builds the plugin, starts/reloads the local stack, and tests registration, org settings, the frontend bundle, both network hops, the plugin proxy, and the app page. Any failure automatically runs the diagnostic collector.

Collect evidence without changing service configuration:

```bash
bash scripts/diagnose_ai_plugin.sh
```

The collector does run typecheck and webpack so it can prove the source is buildable. Otherwise it is read-only. It prints the path to a timestamped report under `.run/reports/`; `.run/reports/latest-ai-plugin` points to the newest report.

## Report layout

- `summary.md`: layer-by-layer PASS/FAIL, first failing layer, exact HTTP statuses, and an evidence-based likely cause.
- `build/`: command output, source/built metadata, checksums/diff, and the `react/jsx-runtime` check.
- `docker/`: container state, safe mount details, Grafana version, and files visible inside Grafana.
- `network/`: separate status, response headers, body, and curl error files for each HTTP request.
- `logs/`: filtered Grafana logs, recent service logs, and the host AI bridge log.

Common first failures are: build/imports, Grafana health, plugin registration, missing/disabled org settings, module serving, host bridge health, Grafana-container networking, route matching, and the app page. A proxy 404 with `plugin route match not found` means Grafana has no loaded matching route; compare all three `plugin.json` copies and restart Grafana. A proxy 500 with `plugin setting not found` is the separate org-settings problem.

## Sharing reports safely

The collector never reads `.env`, never dumps container/process environments, and redacts authorization headers, cookies, bearer tokens, and common password/token/secret fields. The generated `summary.md`, `build/`, `docker/`, `network/`, and `logs/` files are designed to be shareable with another engineer or AI after a quick human review.

Never share `.env`, Grafana service-account tokens, `MCP_GRAFANA_SERVER_TOKEN`, `GRAFANA_MCP_SERVICE_ACCOUNT_TOKEN`, Authorization/Cookie headers, private keys, or raw environment dumps. Reports remain git-ignored under `.run/` and must not be committed.
