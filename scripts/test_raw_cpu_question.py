from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
QUESTION = (
    "Is the raw CPU data in MHz or percentage? "
    "Show me one row/sample of the actual raw CPU data from this local Prometheus datasource."
)


def run(command: list[str], *, env: dict[str, str] | None = None, timeout: int = 900) -> subprocess.CompletedProcess[str]:
    print("+", " ".join(command), flush=True)
    return subprocess.run(
        command,
        cwd=ROOT,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=timeout,
        check=False,
    )


def main() -> int:
    print("=== 1. Start/verify local stack ===", flush=True)
    start = run(["bash", "./start.sh"], timeout=180)
    print(start.stdout, end="", flush=True)
    if start.returncode != 0:
        print("\nRAW CPU GROUNDING TEST: FAIL - stack did not start", file=sys.stderr)
        return 1

    print("\n=== 2. Ask Qwen3 4B for one real raw CPU sample ===", flush=True)
    env = os.environ.copy()
    env["OLLAMA_MODEL"] = "qwen3:4b"
    env["OLLAMA_TEMPERATURE"] = "0"
    result = run([sys.executable, "-m", "agent.main", QUESTION], env=env)
    print(result.stdout, end="", flush=True)

    if result.returncode != 0:
        print("\nRAW CPU GROUNDING TEST: FAIL - agent returned non-zero", file=sys.stderr)
        return 1

    query_pattern = re.compile(
        r"\[qwen\] requesting tool: query_prometheus arguments=.*demo_cpu_usage_percent",
        re.IGNORECASE,
    )
    if not query_pattern.search(result.stdout):
        print(
            "\nRAW CPU GROUNDING TEST: FAIL - agent did not query the actual demo_cpu_usage_percent metric",
            file=sys.stderr,
        )
        return 1

    lower = result.stdout.lower()
    if "percentage" not in lower and "percent" not in lower and "%" not in result.stdout:
        print(
            "\nRAW CPU GROUNDING TEST: FAIL - answer did not identify the CPU metric as percentage-based",
            file=sys.stderr,
        )
        return 1

    if "live-data request could not be grounded" in lower:
        print(
            "\nRAW CPU GROUNDING TEST: FAIL - grounding guard rejected the answer",
            file=sys.stderr,
        )
        return 1

    print("\n=== RAW CPU GROUNDING TEST: PASS ===", flush=True)
    print("Qwen discovered/queryed the real local CPU gauge instead of substituting node_exporter knowledge.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
