import random
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


PORT = 8000
LOCK = threading.Lock()
START_TIME = time.time()
REQUESTS = 0
ERRORS = 0
LATENCY_SUM = 0.0
LATENCY_COUNT = 0
LATENCY_BUCKETS = [0.01, 0.05, 0.1, 0.25, 0.5, 1.0, float("inf")]
LATENCY_COUNTS = [0] * len(LATENCY_BUCKETS)
CPU_USAGE = 20.0
MEMORY_USAGE = 64 * 1024 * 1024


def observe_request(latency, error=False):
    global REQUESTS, ERRORS, LATENCY_SUM, LATENCY_COUNT
    with LOCK:
        REQUESTS += 1
        ERRORS += int(error)
        LATENCY_SUM += latency
        LATENCY_COUNT += 1
        for index, boundary in enumerate(LATENCY_BUCKETS):
            if latency <= boundary:
                LATENCY_COUNTS[index] += 1


def simulate_workload():
    global CPU_USAGE, MEMORY_USAGE
    while True:
        latency = random.uniform(0.01, 0.45)
        error = random.random() < 0.08
        time.sleep(latency)
        observe_request(latency, error)
        with LOCK:
            CPU_USAGE = max(1.0, min(95.0, CPU_USAGE + random.uniform(-8.0, 8.0)))
            MEMORY_USAGE = max(32, min(256, MEMORY_USAGE / (1024 * 1024) + random.uniform(-4.0, 6.0))) * 1024 * 1024


def metric_line(name, value, labels=None):
    if labels:
        label_text = ",".join(f'{key}="{value_}"' for key, value_ in labels.items())
        return f"{name}{{{label_text}}} {value}"
    return f"{name} {value}"


def render_metrics():
    with LOCK:
        requests = REQUESTS
        errors = ERRORS
        latency_sum = LATENCY_SUM
        latency_count = LATENCY_COUNT
        latency_counts = list(LATENCY_COUNTS)
        cpu_usage = CPU_USAGE
        memory_usage = MEMORY_USAGE

    lines = [
        "# HELP demo_http_requests_total Total number of simulated HTTP requests.",
        "# TYPE demo_http_requests_total counter",
        metric_line("demo_http_requests_total", requests),
        "# HELP demo_http_errors_total Total number of simulated HTTP errors.",
        "# TYPE demo_http_errors_total counter",
        metric_line("demo_http_errors_total", errors),
        "# HELP demo_http_request_duration_seconds HTTP request latency in seconds.",
        "# TYPE demo_http_request_duration_seconds histogram",
    ]
    for boundary, count in zip(LATENCY_BUCKETS, latency_counts):
        bucket = "+Inf" if boundary == float("inf") else boundary
        lines.append(metric_line("demo_http_request_duration_seconds_bucket", count, {"le": bucket}))
    lines.extend(
        [
            metric_line("demo_http_request_duration_seconds_sum", latency_sum),
            metric_line("demo_http_request_duration_seconds_count", latency_count),
            "# HELP demo_cpu_usage_percent Simulated CPU-like usage percentage.",
            "# TYPE demo_cpu_usage_percent gauge",
            metric_line("demo_cpu_usage_percent", round(cpu_usage, 2)),
            "# HELP demo_memory_usage_bytes Simulated memory-like usage in bytes.",
            "# TYPE demo_memory_usage_bytes gauge",
            metric_line("demo_memory_usage_bytes", round(memory_usage)),
            "# HELP demo_uptime_seconds Seconds since the demo application started.",
            "# TYPE demo_uptime_seconds gauge",
            metric_line("demo_uptime_seconds", round(time.time() - START_TIME, 2)),
        ]
    )
    return "\n".join(lines) + "\n"


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/metrics":
            body = render_metrics().encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
        elif self.path == "/health":
            body = b"ok\n"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
        elif self.path == "/":
            body = b"demo metrics application\n"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
        else:
            body = b"not found\n"
            self.send_response(404)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):
        return


if __name__ == "__main__":
    threading.Thread(target=simulate_workload, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
