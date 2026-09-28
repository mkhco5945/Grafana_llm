from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from dotenv import load_dotenv


ROOT = Path(__file__).resolve().parents[1]
DASHBOARD_UID = "6b3f0d81-f0e1-4248-ad79-ff6a7f193a1e"
REQUEST_RATE_TITLE = "Request Rate"
REQUEST_RATE_UNIT = "reqps"


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
            raw = response.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")
        raise RuntimeError(f"HTTP {exc.code} from {url}: {detail[:1200]}") from exc


def find_request_rate_panel(dashboard: dict[str, Any]) -> dict[str, Any]:
    panels = dashboard.get("panels")
    if not isinstance(panels, list):
        raise RuntimeError("Dashboard has no top-level panels array")
    matches = [
        panel
        for panel in panels
        if isinstance(panel, dict) and str(panel.get("title") or "").strip() == REQUEST_RATE_TITLE
    ]
    if len(matches) != 1:
        raise RuntimeError(
            f"Expected exactly one '{REQUEST_RATE_TITLE}' panel, found {len(matches)}"
        )
    return matches[0]


def get_legacy_unit(panel: dict[str, Any]) -> str:
    yaxes = panel.get("yaxes")
    if isinstance(yaxes, list) and yaxes and isinstance(yaxes[0], dict):
        return str(yaxes[0].get("format") or "").strip()
    return ""


def main() -> int:
    os.chdir(ROOT)
    load_dotenv(ROOT / ".env")

    token = os.getenv("GRAFANA_MCP_SERVICE_ACCOUNT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("GRAFANA_MCP_SERVICE_ACCOUNT_TOKEN is missing from .env")

    headers = {"Authorization": f"Bearer {token}"}
    url = f"http://127.0.0.1:3000/api/dashboards/uid/{DASHBOARD_UID}"
    payload = request_json(url, headers=headers)
    dashboard = payload.get("dashboard")
    if not isinstance(dashboard, dict):
        raise RuntimeError("Grafana API did not return a dashboard document")

    panel = find_request_rate_panel(dashboard)
    before = get_legacy_unit(panel)
    print(f"Request Rate unit before: {before or '<missing>'}", flush=True)

    yaxes = panel.get("yaxes")
    if not isinstance(yaxes, list):
        yaxes = []
        panel["yaxes"] = yaxes
    if not yaxes:
        yaxes.append({})
    if not isinstance(yaxes[0], dict):
        yaxes[0] = {}
    yaxes[0]["format"] = REQUEST_RATE_UNIT
    yaxes[0].setdefault("label", "Requests/Second")

    meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
    body: dict[str, Any] = {
        "dashboard": dashboard,
        "overwrite": True,
        "message": "Fix Request Rate display unit",
    }
    folder_uid = meta.get("folderUid")
    if isinstance(folder_uid, str) and folder_uid:
        body["folderUid"] = folder_uid

    result = request_json(
        "http://127.0.0.1:3000/api/dashboards/db",
        method="POST",
        body=body,
        headers=headers,
    )
    print(f"Grafana update: {json.dumps(result)}", flush=True)

    verified_payload = request_json(url, headers=headers)
    verified_dashboard = verified_payload.get("dashboard")
    if not isinstance(verified_dashboard, dict):
        raise RuntimeError("Could not retrieve dashboard after update")
    verified_panel = find_request_rate_panel(verified_dashboard)
    after = get_legacy_unit(verified_panel)
    print(f"Request Rate unit after: {after or '<missing>'}", flush=True)
    if after != REQUEST_RATE_UNIT:
        raise RuntimeError(
            f"Expected Request Rate unit '{REQUEST_RATE_UNIT}', got '{after}'"
        )

    print("\n=== Running fast dashboard acceptance ===", flush=True)
    subprocess.run(
        [sys.executable, "scripts/inspect_bad_day_dashboard.py"],
        cwd=ROOT,
        check=True,
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
    except Exception as exc:
        print(f"\nFIX/ACCEPTANCE: FAIL\n{exc}", file=sys.stderr)
        raise SystemExit(1)
