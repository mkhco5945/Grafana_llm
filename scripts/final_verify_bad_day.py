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
EXPECTED_TITLES = {
    "Request Rate",
    "Error Percentage",
    "P95 Request Latency",
    "CPU Usage",
    "Memory Usage",
}

AGENT_PROMPT = f"""The existing Grafana dashboard '{DASHBOARD_TITLE}' with UID {DASHBOARD_UID} must be a real visible incident dashboard.

Do not create a duplicate dashboard and do not change the UID.

Retrieve the actual existing dashboard and repair it if necessary.

It must contain five visible Grafana panels directly in the real dashboard panels array:
- Request Rate
- Error Percentage
- P95 Request Latency
- CPU Usage
- Memory Usage

Use only live discovered Prometheus metrics. Validate every PromQL expression before writing it.

Required semantics:
- Request Rate must use rate() over the request counter.
- Error Percentage must divide the error rate by the request rate and multiply by 100.
- P95 Request Latency must use histogram_quantile() with rate() over the latency _bucket metric.
- CPU Usage must use the real CPU-like gauge.
- Memory Usage must use the real memory-like gauge.

Every panel must be a complete renderable Grafana panel with a unique numeric id, title, visualization type, valid gridPos, Prometheus datasource, at least one target with PromQL, and an appropriate unit.

Update the existing dashboard through Grafana MCP. After the write, retrieve it again. Do not report success until the dashboard structure is renderable and every saved important PromQL expression has been verified against real Prometheus data.

At the end return the dashboard title, UID, URL, panel titles, and final PromQL expressions."""


def run(command: list[str], *, env: dict[str, str] | None = None) -> None:
    print("+", " ".join(command), flush=True)
    subprocess.run(command, cwd=ROOT, env=env, check=True)


def request_json(
    url: str,
    *,
    method: str = "GET",
    body: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 30.0,
) -> Any:
    payload = None
    request_headers = {"Accept": "application/json"}
    if headers:
        request_headers.update(headers)
    if body is not None:
        payload = json.dumps(body).encode()
        request_headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        url,
        data=payload,
        headers=request_headers,
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")
        raise RuntimeError(f"HTTP {exc.code} from {url}: {detail[:1200]}") from exc


def panel_expr(panel: dict[str, Any]) -> str:
    for target in panel.get("targets") or []:
        if isinstance(target, dict):
            expression = target.get("expr")
            if isinstance(expression, str) and expression.strip():
                return expression.strip()
    return ""


def datasource_uid(panel: dict[str, Any]) -> str:
    candidates: list[Any] = [panel.get("datasource")]
    for target in panel.get("targets") or []:
        if isinstance(target, dict):
            candidates.append(target.get("datasource"))
    for value in candidates:
        if isinstance(value, str) and value:
            return value
        if isinstance(value, dict):
            uid = value.get("uid")
            if isinstance(uid, str) and uid:
                return uid
    return ""


def verify_panel_structure(panel: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    title = str(panel.get("title") or "<untitled>")
    if not isinstance(panel.get("id"), int):
        errors.append(f"{title}: id is not numeric")
    if not str(panel.get("type") or "").strip():
        errors.append(f"{title}: missing visualization type")
    grid = panel.get("gridPos")
    if not isinstance(grid, dict):
        errors.append(f"{title}: missing gridPos")
    else:
        for key in ("x", "y", "w", "h"):
            if not isinstance(grid.get(key), int):
                errors.append(f"{title}: gridPos.{key} is missing/not integer")
        if isinstance(grid.get("w"), int) and grid["w"] <= 0:
            errors.append(f"{title}: gridPos.w must be positive")
        if isinstance(grid.get("h"), int) and grid["h"] <= 0:
            errors.append(f"{title}: gridPos.h must be positive")
    if not datasource_uid(panel):
        errors.append(f"{title}: missing datasource")
    if not panel_expr(panel):
        errors.append(f"{title}: missing PromQL target")
    unit = (
        (panel.get("fieldConfig") or {}).get("defaults") or {}
    ).get("unit")
    if not isinstance(unit, str) or not unit.strip():
        errors.append(f"{title}: missing unit")
    return errors


def verify_prometheus(expression: str) -> int:
    query = urllib.parse.urlencode({"query": expression})
    payload = request_json(f"http://127.0.0.1:9090/api/v1/query?{query}")
    if payload.get("status") != "success":
        raise RuntimeError(f"Prometheus query failed: {expression}: {payload}")
    result = ((payload.get("data") or {}).get("result") or [])
    if not result:
        raise RuntimeError(f"Prometheus returned no data for: {expression}")
    return len(result)


def main() -> int:
    os.chdir(ROOT)
    load_dotenv(ROOT / ".env")

    print("\n=== 1. Start/verify local stack ===", flush=True)
    run(["bash", "./start.sh"])

    print("\n=== 2. Force bad-day scenario ===", flush=True)
    scenario = request_json(
        "http://127.0.0.1:8000/scenario/preset",
        method="POST",
        body={"name": "bad-day"},
    )
    print(json.dumps(scenario, indent=2), flush=True)
    wait_seconds = max(6.0, float(os.getenv("DEMO_SCRAPE_WAIT_SECONDS", "6")))
    print(f"Waiting {wait_seconds:.0f}s for Prometheus scrape...", flush=True)
    time.sleep(wait_seconds)

    print("\n=== 3. Run real local Qwen dashboard repair ===", flush=True)
    agent_env = os.environ.copy()
    # The successful acceptance work used deterministic generation for this focused repair.
    agent_env["OLLAMA_TEMPERATURE"] = "0"
    run([sys.executable, "-m", "agent.main", AGENT_PROMPT], env=agent_env)

    print("\n=== 4. Independently inspect real Grafana dashboard JSON ===", flush=True)
    token = os.getenv("GRAFANA_MCP_SERVICE_ACCOUNT_TOKEN", "").strip()
    if not token:
        raise RuntimeError(
            "GRAFANA_MCP_SERVICE_ACCOUNT_TOKEN is missing from .env; cannot independently inspect Grafana"
        )
    payload = request_json(
        f"http://127.0.0.1:3000/api/dashboards/uid/{DASHBOARD_UID}",
        headers={"Authorization": f"Bearer {token}"},
    )
    dashboard = payload.get("dashboard")
    if not isinstance(dashboard, dict):
        raise RuntimeError("Grafana API response did not contain a dashboard document")
    if dashboard.get("uid") != DASHBOARD_UID:
        raise RuntimeError(f"Unexpected dashboard UID: {dashboard.get('uid')}")
    if dashboard.get("title") != DASHBOARD_TITLE:
        raise RuntimeError(f"Unexpected dashboard title: {dashboard.get('title')}")

    panels = dashboard.get("panels")
    if not isinstance(panels, list):
        raise RuntimeError("Dashboard has no top-level panels array")
    actual_titles = {
        str(panel.get("title") or "").strip()
        for panel in panels
        if isinstance(panel, dict)
    }
    missing_titles = EXPECTED_TITLES - actual_titles
    if missing_titles:
        raise RuntimeError(
            f"Dashboard is missing expected visible panels: {sorted(missing_titles)}; "
            f"actual={sorted(actual_titles)}"
        )
    if len(panels) < 5:
        raise RuntimeError(f"Expected at least 5 visible panels, found {len(panels)}")

    structural_errors: list[str] = []
    checked: list[tuple[str, str, str, str, int]] = []
    for panel in panels:
        if not isinstance(panel, dict):
            structural_errors.append("Non-object item found in panels array")
            continue
        title = str(panel.get("title") or "").strip()
        if title not in EXPECTED_TITLES:
            continue
        structural_errors.extend(verify_panel_structure(panel))
        expression = panel_expr(panel)
        series_count = verify_prometheus(expression) if expression else 0
        grid = panel.get("gridPos") or {}
        checked.append(
            (
                title,
                str(panel.get("type") or ""),
                expression,
                str(((panel.get("fieldConfig") or {}).get("defaults") or {}).get("unit") or ""),
                series_count,
            )
        )
        print(
            f"PASS panel: {title} | type={panel.get('type')} | "
            f"grid={grid} | datasource={datasource_uid(panel)} | "
            f"unit={((panel.get('fieldConfig') or {}).get('defaults') or {}).get('unit')}\n"
            f"  PromQL: {expression}\n"
            f"  Prometheus series: {series_count}",
            flush=True,
        )

    if structural_errors:
        raise RuntimeError("Dashboard structure errors:\n- " + "\n- ".join(structural_errors))
    if len(checked) != 5:
        raise RuntimeError(f"Expected to verify 5 named panels, verified {len(checked)}")

    print("\n=== FINAL ACCEPTANCE: PASS ===", flush=True)
    print(f"Dashboard: {DASHBOARD_TITLE}", flush=True)
    print(f"UID:       {DASHBOARD_UID}", flush=True)
    print(
        f"URL:       http://localhost:3000/d/{DASHBOARD_UID}/bad-day-anomaly-detection",
        flush=True,
    )
    print(f"Visible top-level panels: {len(panels)}", flush=True)
    print("All five required panels are structurally renderable and return live Prometheus data.", flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        raise SystemExit(130)
    except Exception as exc:
        print(f"\nFINAL ACCEPTANCE: FAIL\n{exc}", file=sys.stderr)
        raise SystemExit(1)
