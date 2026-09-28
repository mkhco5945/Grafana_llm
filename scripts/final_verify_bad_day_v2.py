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


def run(command: list[str]) -> None:
    print("+", " ".join(command), flush=True)
    subprocess.run(command, cwd=ROOT, check=True)


def normalize_panels_array() -> None:
    token = os.getenv("GRAFANA_MCP_SERVICE_ACCOUNT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("GRAFANA_MCP_SERVICE_ACCOUNT_TOKEN is missing from .env")

    headers = {"Authorization": f"Bearer {token}"}
    payload = request_json(
        f"http://127.0.0.1:3000/api/dashboards/uid/{DASHBOARD_UID}",
        headers=headers,
    )
    dashboard = payload.get("dashboard")
    if not isinstance(dashboard, dict):
        raise RuntimeError("Grafana API did not return a dashboard document")

    panels = dashboard.get("panels")
    if isinstance(panels, list):
        print(
            f"[preflight] Dashboard already has a valid top-level panels array "
            f"({len(panels)} item(s)); no normalization needed.",
            flush=True,
        )
        return

    print(
        "[preflight] Existing dashboard has a non-array/missing top-level panels field. "
        "Normalizing ONLY that field to an empty array so Qwen can populate it via MCP.",
        flush=True,
    )
    dashboard["panels"] = []

    meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
    body: dict[str, Any] = {
        "dashboard": dashboard,
        "overwrite": True,
        "message": "Normalize broken panels container before local AI repair",
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
    print(f"[preflight] Grafana normalization result: {json.dumps(result)}", flush=True)

    check = request_json(
        f"http://127.0.0.1:3000/api/dashboards/uid/{DASHBOARD_UID}",
        headers=headers,
    )
    saved = check.get("dashboard")
    if not isinstance(saved, dict) or not isinstance(saved.get("panels"), list):
        raise RuntimeError("Preflight normalization failed: top-level panels is still not an array")
    print("[preflight] Confirmed top-level panels is now an array.", flush=True)


def main() -> int:
    os.chdir(ROOT)
    load_dotenv(ROOT / ".env")

    print("=== PRE-FLIGHT: start stack ===", flush=True)
    run(["bash", "./start.sh"])

    print("\n=== PRE-FLIGHT: normalize broken dashboard container if needed ===", flush=True)
    normalize_panels_array()

    print("\n=== RUN EXISTING FULL ACCEPTANCE ===", flush=True)
    run([sys.executable, "scripts/final_verify_bad_day.py"])
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        raise SystemExit(130)
    except Exception as exc:
        print(f"\nFINAL V2 ACCEPTANCE: FAIL\n{exc}", file=sys.stderr)
        raise SystemExit(1)
