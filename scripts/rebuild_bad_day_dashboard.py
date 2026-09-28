from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from dotenv import load_dotenv


ROOT = Path(__file__).resolve().parents[1]
DASHBOARD_UID = "6b3f0d81-f0e1-4248-ad79-ff6a7f193a1e"
DASHBOARD_TITLE = "Bad Day Anomaly Detection"
GRAFANA_URL = "http://127.0.0.1:3000"
PROMETHEUS_URL = "http://127.0.0.1:9090"
DEMO_URL = "http://127.0.0.1:8000"
DATASOURCE = {"type": "prometheus", "uid": "prometheus"}

EXPRESSIONS = {
    "CPU Usage": "demo_cpu_usage_percent",
    "Error Percentage": '(rate(demo_http_errors_total{job="demo"}[30s]) / rate(demo_http_requests_total{job="demo"}[30s])) * 100',
    "Request Rate": 'rate(demo_http_requests_total{job="demo"}[30s])',
    "P95 Request Latency": 'histogram_quantile(0.95, sum by (le) (rate(demo_http_request_duration_seconds_bucket{job="demo"}[30s])))',
    "Memory Usage": "demo_memory_usage_bytes",
}

UNITS = {
    "CPU Usage": "percent",
    "Error Percentage": "percent",
    "Request Rate": "reqps",
    "P95 Request Latency": "s",
    "Memory Usage": "bytes",
}

LAYOUT = {
    "CPU Usage": {"x": 0, "y": 0, "w": 12, "h": 8},
    "Error Percentage": {"x": 12, "y": 0, "w": 12, "h": 8},
    "Request Rate": {"x": 0, "y": 8, "w": 12, "h": 8},
    "P95 Request Latency": {"x": 12, "y": 8, "w": 12, "h": 8},
    "Memory Usage": {"x": 0, "y": 16, "w": 24, "h": 8},
}

THRESHOLDS = {
    "CPU Usage": [(None, "green"), (70, "yellow"), (85, "red")],
    "Error Percentage": [(None, "green"), (5, "yellow"), (20, "red")],
    "Request Rate": [(None, "green")],
    "P95 Request Latency": [(None, "green"), (0.5, "yellow"), (1.0, "red")],
    "Memory Usage": [(None, "green"), (150 * 1024 * 1024, "yellow"), (200 * 1024 * 1024, "red")],
}

DESCRIPTIONS = {
    "CPU Usage": "Simulated service CPU pressure. Bad-day drives the baseline into the critical range.",
    "Error Percentage": "Errors divided by requests over a responsive 30-second rate window.",
    "Request Rate": "Observed request throughput over the last 30 seconds.",
    "P95 Request Latency": "95th percentile request latency from the Prometheus histogram buckets.",
    "Memory Usage": "Simulated service memory pressure. Bad-day raises the baseline and adds growth per request.",
}

LEGENDS = {
    "CPU Usage": "CPU",
    "Error Percentage": "Errors",
    "Request Rate": "Requests",
    "P95 Request Latency": "p95 latency",
    "Memory Usage": "Memory",
}


class Failure(RuntimeError):
    pass


def request_json(
    url: str,
    *,
    method: str = "GET",
    body: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 30.0,
) -> Any:
    data = None
    request_headers = {"Accept": "application/json"}
    if headers:
        request_headers.update(headers)
    if body is not None:
        data = json.dumps(body).encode()
        request_headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=request_headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")
        raise Failure(f"HTTP {exc.code} from {url}: {detail[:1500]}") from exc


def run(command: list[str]) -> None:
    print("+", " ".join(command), flush=True)
    subprocess.run(command, cwd=ROOT, check=True)


def auth_headers() -> dict[str, str]:
    token = os.getenv("GRAFANA_MCP_SERVICE_ACCOUNT_TOKEN", "").strip()
    if not token:
        raise Failure("GRAFANA_MCP_SERVICE_ACCOUNT_TOKEN is missing from .env")
    return {"Authorization": f"Bearer {token}"}


def set_preset(name: str) -> dict[str, Any]:
    return request_json(
        f"{DEMO_URL}/scenario/preset",
        method="POST",
        body={"name": name},
    )


def prometheus_instant(expression: str) -> list[dict[str, Any]]:
    query = urllib.parse.urlencode({"query": expression})
    payload = request_json(f"{PROMETHEUS_URL}/api/v1/query?{query}")
    if payload.get("status") != "success":
        raise Failure(f"Prometheus query failed for {expression!r}: {payload}")
    result = ((payload.get("data") or {}).get("result") or [])
    if not result:
        raise Failure(f"Prometheus returned no data for {expression!r}")
    return result


def first_numeric_value(expression: str) -> float:
    result = prometheus_instant(expression)
    value = result[0].get("value")
    if not isinstance(value, list) or len(value) < 2:
        raise Failure(f"Unexpected Prometheus result for {expression!r}: {result[0]}")
    return float(value[1])


def threshold_config(title: str) -> dict[str, Any]:
    return {
        "mode": "absolute",
        "steps": [
            {"color": color, "value": value}
            for value, color in THRESHOLDS[title]
        ],
    }


def timeseries_panel(panel_id: int, title: str) -> dict[str, Any]:
    defaults: dict[str, Any] = {
        "color": {"mode": "palette-classic"},
        "custom": {
            "axisCenteredZero": False,
            "axisColorMode": "text",
            "axisLabel": "",
            "axisPlacement": "auto",
            "barAlignment": 0,
            "drawStyle": "line",
            "fillOpacity": 12,
            "gradientMode": "none",
            "hideFrom": {"legend": False, "tooltip": False, "viz": False},
            "lineInterpolation": "smooth",
            "lineWidth": 2,
            "pointSize": 5,
            "scaleDistribution": {"type": "linear"},
            "showPoints": "never",
            "spanNulls": False,
            "stacking": {"group": "A", "mode": "none"},
            "thresholdsStyle": {"mode": "line"},
        },
        "mappings": [],
        "thresholds": threshold_config(title),
        "unit": UNITS[title],
    }
    if title in {"CPU Usage", "Error Percentage"}:
        defaults["min"] = 0
        defaults["max"] = 100
    elif title in {"Request Rate", "P95 Request Latency", "Memory Usage"}:
        defaults["min"] = 0

    return {
        "id": panel_id,
        "title": title,
        "description": DESCRIPTIONS[title],
        "type": "timeseries",
        "datasource": dict(DATASOURCE),
        "gridPos": dict(LAYOUT[title]),
        "fieldConfig": {"defaults": defaults, "overrides": []},
        "options": {
            "legend": {
                "calcs": ["lastNotNull", "max"],
                "displayMode": "list",
                "placement": "bottom",
                "showLegend": True,
            },
            "tooltip": {"mode": "single", "sort": "none"},
        },
        "targets": [
            {
                "datasource": dict(DATASOURCE),
                "editorMode": "code",
                "expr": EXPRESSIONS[title],
                "legendFormat": LEGENDS[title],
                "range": True,
                "refId": "A",
            }
        ],
    }


def build_panels() -> list[dict[str, Any]]:
    titles = [
        "CPU Usage",
        "Error Percentage",
        "Request Rate",
        "P95 Request Latency",
        "Memory Usage",
    ]
    return [timeseries_panel(index + 1, title) for index, title in enumerate(titles)]


def update_dashboard() -> dict[str, Any]:
    headers = auth_headers()
    payload = request_json(
        f"{GRAFANA_URL}/api/dashboards/uid/{DASHBOARD_UID}",
        headers=headers,
    )
    dashboard = payload.get("dashboard")
    if not isinstance(dashboard, dict):
        raise Failure("Grafana API did not return the existing dashboard document")
    if dashboard.get("uid") != DASHBOARD_UID:
        raise Failure(f"Unexpected dashboard UID: {dashboard.get('uid')}")

    dashboard["title"] = DASHBOARD_TITLE
    dashboard["panels"] = build_panels()
    dashboard["time"] = {"from": "now-10m", "to": "now"}
    dashboard["refresh"] = "5s"
    dashboard["timezone"] = "browser"
    dashboard["tags"] = sorted(set((dashboard.get("tags") or []) + ["demo", "anomaly", "bad-day"]))

    meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
    body: dict[str, Any] = {
        "dashboard": dashboard,
        "overwrite": True,
        "message": "Rebuild bad-day dashboard with modern anomaly panels",
    }
    folder_uid = meta.get("folderUid")
    if isinstance(folder_uid, str) and folder_uid:
        body["folderUid"] = folder_uid

    result = request_json(
        f"{GRAFANA_URL}/api/dashboards/db",
        method="POST",
        body=body,
        headers=headers,
    )
    if result.get("status") != "success":
        raise Failure(f"Grafana dashboard update did not succeed: {result}")
    return result


def verify_dashboard() -> None:
    payload = request_json(
        f"{GRAFANA_URL}/api/dashboards/uid/{DASHBOARD_UID}",
        headers=auth_headers(),
    )
    dashboard = payload.get("dashboard")
    if not isinstance(dashboard, dict):
        raise Failure("Grafana verification response has no dashboard document")

    panels = dashboard.get("panels")
    if not isinstance(panels, list) or len(panels) != 5:
        raise Failure(f"Expected exactly 5 top-level panels, found {len(panels) if isinstance(panels, list) else 'none'}")

    expected_titles = set(EXPRESSIONS)
    actual_titles = {str(panel.get("title") or "") for panel in panels if isinstance(panel, dict)}
    if actual_titles != expected_titles:
        raise Failure(f"Panel title mismatch: expected={sorted(expected_titles)} actual={sorted(actual_titles)}")

    seen_ids: set[int] = set()
    for panel in panels:
        if not isinstance(panel, dict):
            raise Failure("Non-object panel found")
        title = str(panel.get("title") or "")
        panel_id = panel.get("id")
        if not isinstance(panel_id, int) or panel_id in seen_ids:
            raise Failure(f"{title}: panel id is missing/duplicate: {panel_id}")
        seen_ids.add(panel_id)
        if panel.get("type") != "timeseries":
            raise Failure(f"{title}: expected type=timeseries, got {panel.get('type')!r}")
        if panel.get("gridPos") != LAYOUT[title]:
            raise Failure(f"{title}: unexpected gridPos {panel.get('gridPos')}")
        datasource = panel.get("datasource")
        if not isinstance(datasource, dict) or datasource.get("uid") != "prometheus":
            raise Failure(f"{title}: wrong panel datasource {datasource}")
        defaults = ((panel.get("fieldConfig") or {}).get("defaults") or {})
        if defaults.get("unit") != UNITS[title]:
            raise Failure(f"{title}: expected unit {UNITS[title]!r}, got {defaults.get('unit')!r}")
        targets = panel.get("targets")
        if not isinstance(targets, list) or len(targets) != 1:
            raise Failure(f"{title}: expected one target")
        target = targets[0]
        if target.get("expr") != EXPRESSIONS[title]:
            raise Failure(f"{title}: unexpected PromQL {target.get('expr')!r}")
        target_ds = target.get("datasource")
        if not isinstance(target_ds, dict) or target_ds.get("uid") != "prometheus":
            raise Failure(f"{title}: wrong target datasource {target_ds}")
        series = prometheus_instant(EXPRESSIONS[title])
        print(
            f"PASS {title}: type=timeseries grid={panel['gridPos']} unit={UNITS[title]} series={len(series)}\n"
            f"  {EXPRESSIONS[title]}",
            flush=True,
        )

    if dashboard.get("time") != {"from": "now-10m", "to": "now"}:
        raise Failure(f"Unexpected dashboard time range: {dashboard.get('time')}")
    if dashboard.get("refresh") != "5s":
        raise Failure(f"Unexpected refresh: {dashboard.get('refresh')!r}")


def capture_state(label: str) -> dict[str, float]:
    values = {
        "cpu": first_numeric_value(EXPRESSIONS["CPU Usage"]),
        "memory": first_numeric_value(EXPRESSIONS["Memory Usage"]),
        "error_pct": first_numeric_value(EXPRESSIONS["Error Percentage"]),
        "p95": first_numeric_value(EXPRESSIONS["P95 Request Latency"]),
    }
    print(
        f"[{label}] CPU={values['cpu']:.1f}% | error={values['error_pct']:.1f}% | "
        f"p95={values['p95']:.3f}s | memory={values['memory'] / 1024 / 1024:.1f} MiB",
        flush=True,
    )
    return values


def verify_transition(normal: dict[str, float], bad: dict[str, float]) -> None:
    problems: list[str] = []
    if bad["cpu"] < normal["cpu"] + 30:
        problems.append(f"CPU did not rise enough ({normal['cpu']:.1f} -> {bad['cpu']:.1f})")
    if bad["memory"] < normal["memory"] + 80 * 1024 * 1024:
        problems.append("memory did not rise by at least 80 MiB")
    if bad["p95"] < normal["p95"] + 0.20:
        problems.append(f"p95 did not rise enough ({normal['p95']:.3f} -> {bad['p95']:.3f})")
    if bad["error_pct"] < normal["error_pct"] + 3:
        problems.append(f"error percentage did not rise enough ({normal['error_pct']:.1f} -> {bad['error_pct']:.1f})")
    if problems:
        raise Failure("Bad-day transition was not obvious enough:\n- " + "\n- ".join(problems))


def main() -> int:
    os.chdir(ROOT)
    load_dotenv(ROOT / ".env")
    wait_seconds = max(30.0, float(os.getenv("DEMO_TRANSITION_WAIT_SECONDS", "35")))

    print("=== 1. Start stack ===", flush=True)
    run(["bash", "./start.sh"])

    print("\n=== 2. Rebuild dashboard as modern timeseries anomaly view ===", flush=True)
    result = update_dashboard()
    print(f"Grafana update: {json.dumps(result)}", flush=True)

    print("\n=== 3. Create visible normal baseline ===", flush=True)
    print(json.dumps(set_preset("normal"), indent=2), flush=True)
    print(f"Waiting {wait_seconds:.0f}s to fill the 30s PromQL windows with normal data...", flush=True)
    time.sleep(wait_seconds)
    normal = capture_state("normal")

    print("\n=== 4. Trigger bad-day anomaly ===", flush=True)
    print(json.dumps(set_preset("bad-day"), indent=2), flush=True)
    print(f"Waiting {wait_seconds:.0f}s to fill the 30s PromQL windows with bad-day data...", flush=True)
    time.sleep(wait_seconds)
    bad = capture_state("bad-day")
    verify_transition(normal, bad)
    print("PASS anomaly transition: CPU, memory, p95 and error percentage all moved materially.", flush=True)

    print("\n=== 5. Verify saved dashboard + live Prometheus data ===", flush=True)
    verify_dashboard()

    print("\n=== FINAL ANOMALY DASHBOARD: PASS ===", flush=True)
    print(f"Dashboard: {DASHBOARD_TITLE}", flush=True)
    print(f"UID:       {DASHBOARD_UID}", flush=True)
    print(
        f"URL:       http://localhost:3000/d/{DASHBOARD_UID}/bad-day-anomaly-detection",
        flush=True,
    )
    print("Layout:    five modern timeseries panels, 10-minute window, 5-second refresh", flush=True)
    print("Scenario:  bad-day (left active so you can open Grafana immediately)", flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        raise SystemExit(130)
    except Exception as exc:
        print(f"\n=== FINAL ANOMALY DASHBOARD: FAIL ===\n{exc}", file=sys.stderr)
        raise SystemExit(1)
