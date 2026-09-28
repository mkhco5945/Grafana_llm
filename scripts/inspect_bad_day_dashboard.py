from __future__ import annotations

import json
import os
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


def request_json(url: str, *, headers: dict[str, str] | None = None, timeout: float = 30.0) -> Any:
    req = urllib.request.Request(url, headers=headers or {}, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")
        raise RuntimeError(f"HTTP {exc.code} from {url}: {detail[:1200]}") from exc


def panel_expr(panel: dict[str, Any]) -> str:
    for target in panel.get("targets") or []:
        if isinstance(target, dict):
            expr = target.get("expr")
            if isinstance(expr, str) and expr.strip():
                return expr.strip()
    return ""


def datasource_uid(panel: dict[str, Any]) -> str:
    candidates: list[Any] = [panel.get("datasource")]
    for target in panel.get("targets") or []:
        if isinstance(target, dict):
            candidates.append(target.get("datasource"))
    for value in candidates:
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, dict):
            uid = value.get("uid") or value.get("type")
            if isinstance(uid, str) and uid.strip():
                return uid.strip()
    return ""


def panel_unit(panel: dict[str, Any]) -> str:
    field_config = panel.get("fieldConfig")
    if isinstance(field_config, dict):
        defaults = field_config.get("defaults")
        if isinstance(defaults, dict):
            unit = defaults.get("unit")
            if isinstance(unit, str) and unit.strip():
                return unit.strip()
    yaxes = panel.get("yaxes")
    if isinstance(yaxes, list) and yaxes and isinstance(yaxes[0], dict):
        fmt = yaxes[0].get("format")
        if isinstance(fmt, str) and fmt.strip():
            return fmt.strip()
    return ""


def unit_ok(title: str, unit: str) -> bool:
    t = title.lower()
    u = unit.lower()
    if "request" in t and "rate" in t:
        return u in {"reqps", "ops", "cps", "count/s", "requests/s", "rps"}
    if "error" in t and ("percent" in t or "percentage" in t or "%" in title):
        return u in {"percent", "percentunit", "%"}
    if "cpu" in t:
        return u in {"percent", "percentunit", "%"}
    if "latency" in t or "duration" in t:
        return u in {"s", "sec", "second", "seconds"}
    if "memory" in t:
        return "byte" in u or u in {"decbytes", "bytes"}
    return bool(unit)


def verify_prometheus(expr: str) -> int:
    qs = urllib.parse.urlencode({"query": expr})
    payload = request_json(f"http://127.0.0.1:9090/api/v1/query?{qs}")
    if payload.get("status") != "success":
        raise RuntimeError(f"Prometheus query failed: {expr}: {payload}")
    result = ((payload.get("data") or {}).get("result") or [])
    if not result:
        raise RuntimeError(f"Prometheus returned no data for: {expr}")
    return len(result)


def main() -> int:
    os.chdir(ROOT)
    load_dotenv(ROOT / ".env")
    token = os.getenv("GRAFANA_MCP_SERVICE_ACCOUNT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("GRAFANA_MCP_SERVICE_ACCOUNT_TOKEN is missing from .env")

    payload = request_json(
        f"http://127.0.0.1:3000/api/dashboards/uid/{DASHBOARD_UID}",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
    )
    dashboard = payload.get("dashboard")
    if not isinstance(dashboard, dict):
        raise RuntimeError("Grafana API response did not contain a dashboard document")

    panels = dashboard.get("panels")
    if not isinstance(panels, list):
        raise RuntimeError("Dashboard top-level panels is not an array")

    print(f"Dashboard: {dashboard.get('title')} ({dashboard.get('uid')})")
    print(f"Top-level panel count: {len(panels)}")

    actual_titles = {
        str(panel.get("title") or "").strip()
        for panel in panels
        if isinstance(panel, dict)
    }
    problems: list[str] = []
    missing = EXPECTED_TITLES - actual_titles
    if missing:
        problems.append(f"missing expected panels: {sorted(missing)}")

    seen_ids: set[int] = set()
    checked = 0
    for panel in panels:
        if not isinstance(panel, dict):
            problems.append("non-object item exists in panels array")
            continue
        title = str(panel.get("title") or "").strip()
        if title not in EXPECTED_TITLES:
            continue
        checked += 1
        pid = panel.get("id")
        ptype = str(panel.get("type") or "").strip()
        grid = panel.get("gridPos")
        ds = datasource_uid(panel)
        expr = panel_expr(panel)
        unit = panel_unit(panel)

        if not isinstance(pid, int) or isinstance(pid, bool):
            problems.append(f"{title}: invalid numeric id")
        elif pid in seen_ids:
            problems.append(f"{title}: duplicate panel id {pid}")
        else:
            seen_ids.add(pid)
        if not ptype:
            problems.append(f"{title}: missing visualization type")
        if not isinstance(grid, dict) or not all(isinstance(grid.get(k), int) for k in ("x", "y", "w", "h")):
            problems.append(f"{title}: invalid gridPos")
        elif grid["w"] <= 0 or grid["h"] <= 0 or grid["x"] < 0 or grid["y"] < 0:
            problems.append(f"{title}: invalid gridPos values {grid}")
        if not ds:
            problems.append(f"{title}: missing datasource")
        if not expr:
            problems.append(f"{title}: missing PromQL")
            series = 0
        else:
            series = verify_prometheus(expr)
        if not unit:
            problems.append(f"{title}: missing unit")
        elif not unit_ok(title, unit):
            problems.append(f"{title}: inappropriate unit '{unit}'")

        print(f"\n{title}")
        print(f"  id={pid} type={ptype} grid={grid} datasource={ds} unit={unit or '<missing>'}")
        print(f"  PromQL: {expr}")
        print(f"  Prometheus series: {series}")

    if checked != 5:
        problems.append(f"expected to inspect 5 named panels, inspected {checked}")

    if problems:
        print("\n=== FAST INSPECTION: FAIL ===")
        for problem in problems:
            print(f"- {problem}")
        return 1

    print("\n=== FAST INSPECTION: PASS ===")
    print("All five required visible panels are structurally valid, have appropriate units, and return live Prometheus data.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"\nFAST INSPECTION: ERROR\n{exc}")
        raise SystemExit(2)
