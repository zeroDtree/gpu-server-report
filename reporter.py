"""GPU metrics agent — collects aggregates via nvidia-smi, optional HTTP, optional upstream POST."""

from __future__ import annotations

import argparse
import csv
import json
import os
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlparse

import requests
from dotenv import load_dotenv

from health_server import HealthState, load_health_bind, start_health_server

ENV_AGENT_HTTP_HOST = "AGENT_HTTP_HOST"
ENV_AGENT_HTTP_PORT = "AGENT_HTTP_PORT"
DEFAULT_AGENT_HTTP_HOST = "0.0.0.0"
DEFAULT_AGENT_HTTP_PORT = 9090

NVIDIA_SMI_QUERY = [
    "nvidia-smi",
    "--query-gpu=index,name,utilization.gpu,memory.used,memory.total",
    "--format=csv,noheader,nounits",
]

NVIDIA_SMI_GPU_NAME_QUERY = [
    "nvidia-smi",
    "--query-gpu=name",
    "--format=csv,noheader,nounits",
]


def get_first_gpu_product_name() -> str | None:
    """Return the first GPU product string from nvidia-smi --query-gpu=name, or None on failure."""
    try:
        proc = subprocess.run(
            NVIDIA_SMI_GPU_NAME_QUERY,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    lines = [ln.strip() for ln in proc.stdout.strip().splitlines() if ln.strip()]
    if not lines:
        return None
    name = lines[0].strip()
    return name or None


@dataclass
class PerGpu:
    index: int
    name: str
    avg_util: float  # 0.0 ~ 1.0
    mem_used_mb: int
    mem_total_mb: int


@dataclass
class GpuMetricsSnapshot:
    gpus: list[PerGpu]


def load_config() -> dict:
    load_dotenv()

    raw_level = os.getenv("AGENT_RESOURCE_LEVEL")
    if raw_level is not None and raw_level.strip() != "":
        resource_level = raw_level.strip()
    else:
        detected = get_first_gpu_product_name()
        if detected is not None:
            resource_level = detected
            print(
                "AGENT_RESOURCE_LEVEL unset; using first GPU name from nvidia-smi "
                f"({resource_level!r}).",
                file=sys.stderr,
            )
        else:
            resource_level = "unknown"
            print(
                "WARNING: AGENT_RESOURCE_LEVEL unset and nvidia-smi did not return a GPU name; "
                f"using resourceLevel={resource_level!r}",
                file=sys.stderr,
            )

    try:
        interval = int(os.getenv("AGENT_REPORT_INTERVAL", "30"))
    except ValueError:
        interval = 30

    if interval < 5:
        print("WARNING: AGENT_REPORT_INTERVAL below 5s, clamping to 5s", file=sys.stderr)
        interval = 5

    http_host_raw = os.getenv(ENV_AGENT_HTTP_HOST, DEFAULT_AGENT_HTTP_HOST)
    http_host = http_host_raw.strip() or DEFAULT_AGENT_HTTP_HOST

    try:
        http_port = int(os.getenv(ENV_AGENT_HTTP_PORT, str(DEFAULT_AGENT_HTTP_PORT)))
    except ValueError:
        print(
            f"WARNING: {ENV_AGENT_HTTP_PORT} is not a valid integer, "
            f"using {DEFAULT_AGENT_HTTP_PORT}",
            file=sys.stderr,
        )
        http_port = DEFAULT_AGENT_HTTP_PORT
    if not (1 <= http_port <= 65535):
        print(
            f"WARNING: {ENV_AGENT_HTTP_PORT}={http_port} out of range, "
            f"using {DEFAULT_AGENT_HTTP_PORT}",
            file=sys.stderr,
        )
        http_port = DEFAULT_AGENT_HTTP_PORT

    report_api = os.getenv("REPORT_API_URL", "").strip()
    gsad_api = os.getenv("GSAD_API_URL", "http://localhost:8080").strip()
    api_url = (report_api or gsad_api).rstrip("/")

    return {
        "api_url": api_url,
        "psk": os.getenv("AGENT_PSK", ""),
        "hostname": os.getenv("AGENT_HOSTNAME") or socket.gethostname(),
        "resource_level": resource_level,
        "interval": interval,
        "http_host": http_host,
        "http_port": http_port,
    }


def collect_gpu_snapshot() -> GpuMetricsSnapshot | None:
    """Run nvidia-smi and return per-GPU rows, or None when no GPU is found."""
    try:
        proc = subprocess.run(
            NVIDIA_SMI_QUERY,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except FileNotFoundError:
        print("WARNING: nvidia-smi not found — is this a GPU node?", file=sys.stderr)
        return None
    except subprocess.TimeoutExpired:
        print("WARNING: nvidia-smi timed out", file=sys.stderr)
        return None

    if proc.returncode != 0:
        print(f"WARNING: nvidia-smi exited with {proc.returncode}: {proc.stderr.strip()}", file=sys.stderr)
        return None

    lines = [ln.strip() for ln in proc.stdout.strip().splitlines() if ln.strip()]
    if not lines:
        return None

    gpus: list[PerGpu] = []
    for line in lines:
        try:
            row = next(csv.reader([line]))
        except StopIteration:
            continue
        if len(row) < 5:
            continue
        try:
            idx = int(row[0].strip())
            name = row[1].strip()
            util_pct = float(row[2].strip())
            mem_used = int(row[3].strip())
            mem_total = int(row[4].strip())
        except (ValueError, IndexError):
            continue
        gpus.append(
            PerGpu(
                index=idx,
                name=name,
                avg_util=util_pct / 100.0,
                mem_used_mb=mem_used,
                mem_total_mb=mem_total,
            )
        )

    if not gpus:
        return None

    return GpuMetricsSnapshot(gpus=gpus)


def snapshot_to_public_dict(config: dict, snapshot: GpuMetricsSnapshot) -> dict[str, Any]:
    """Build the canonical JSON object for --print, --push, and GET /metrics."""
    n = len(snapshot.gpus)
    total_util = sum(g.avg_util for g in snapshot.gpus)
    total_mem = sum(g.mem_used_mb for g in snapshot.gpus)
    return {
        "hostname": config["hostname"],
        "resourceLevel": config["resource_level"],
        "collectedAt": datetime.now(UTC).isoformat(),
        "gpus": [
            {
                "index": g.index,
                "name": g.name,
                "avgUtil": g.avg_util,
                "memUsedMb": g.mem_used_mb,
                "memTotalMb": g.mem_total_mb,
            }
            for g in snapshot.gpus
        ],
        "summary": {
            "gpuCount": n,
            "avgUtil": total_util / n,
            "avgMemUsedMb": total_mem // n,
        },
    }


def report(session: requests.Session, config: dict, payload: dict[str, Any]) -> bool:
    url = f"{config['api_url']}/api/internal/servers/report"
    try:
        resp = session.post(
            url,
            json=payload,
            headers={"X-Agent-PSK": config["psk"]},
            timeout=15,
        )
        resp.raise_for_status()
        return True
    except requests.RequestException as exc:
        print(f"ERROR: report failed: {exc}", file=sys.stderr)
        return False


def parse_args(argv: list[str] | None = None, *, config: dict) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="GPU metrics agent (print / HTTP / upstream push).")
    parser.add_argument(
        "--print",
        dest="do_print",
        action="store_true",
        help="Print the same JSON as GET /metrics and --push (one line per interval).",
    )
    parser.add_argument(
        "--serve",
        action="store_true",
        help="Expose GET /metrics and GET /health over HTTP.",
    )
    parser.add_argument(
        "--push",
        action="store_true",
        help="POST metrics to the upstream report API on each collection interval.",
    )
    parser.add_argument(
        "--http-host",
        default=config["http_host"],
        help=(
            "Bind address for --serve "
            f"(default: env {ENV_AGENT_HTTP_HOST!r}, else {DEFAULT_AGENT_HTTP_HOST!r})."
        ),
    )
    parser.add_argument(
        "--http-port",
        type=int,
        default=config["http_port"],
        help=(
            "Port for --serve "
            f"(default: env {ENV_AGENT_HTTP_PORT!r}, else {DEFAULT_AGENT_HTTP_PORT})."
        ),
    )
    args = parser.parse_args(argv)
    if not (args.do_print or args.serve or args.push):
        args.push = True
    return args


def _make_metrics_handler(config: dict) -> type[BaseHTTPRequestHandler]:
    class MetricsHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            del format, args

        def _send_json(self, status: int, body: dict) -> None:
            data = json.dumps(body).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:
            path = urlparse(self.path).path
            if path == "/metrics":
                snapshot = collect_gpu_snapshot()
                if snapshot is None:
                    self._send_json(
                        503,
                        {"ok": False, "error": "gpu_metrics_unavailable"},
                    )
                    return
                self._send_json(200, snapshot_to_public_dict(config, snapshot))
                return
            self._send_json(404, {"ok": False, "error": "not_found"})

    return MetricsHandler


def start_http_server(config: dict, host: str, port: int) -> None:
    handler = _make_metrics_handler(config)
    server = ThreadingHTTPServer((host, port), handler)
    thread = threading.Thread(target=server.serve_forever, name="metrics-http", daemon=True)
    thread.start()
    print(f"HTTP server — http://{host}:{port}/metrics", file=sys.stderr)


def main() -> None:
    config = load_config()
    args = parse_args(config=config)

    if args.push and not config["psk"]:
        print("FATAL: AGENT_PSK is required when --push is enabled", file=sys.stderr)
        sys.exit(1)

    health: HealthState | None = None
    bind = load_health_bind(default_port=9092)
    if bind is not None:
        host, port = bind
        health = HealthState(agent="gpu-server-report", hostname=config["hostname"])
        start_health_server(health, host, port)

    session: requests.Session | None = None
    if args.push:
        session = requests.Session()
        session.headers.update({"Content-Type": "application/json"})

    if args.serve:
        start_http_server(config, args.http_host, args.http_port)

    mode_bits = []
    if args.do_print:
        mode_bits.append("print")
    if args.serve:
        mode_bits.append("serve")
    if args.push:
        mode_bits.append("push")
    modes = "+".join(mode_bits)

    startup_parts = [
        "GPU metrics agent starting",
        f"modes={modes}",
        f"hostname={config['hostname']}",
        f"level={config['resource_level']}",
    ]
    if args.do_print or args.push:
        startup_parts.append(f"interval={config['interval']}s")
    if args.push:
        startup_parts.append(f"endpoint={config['api_url']}/api/internal/servers/report")
    print(" — ".join(startup_parts))

    interval_loop = args.do_print or args.push
    if interval_loop:
        while True:
            snapshot = collect_gpu_snapshot()

            if snapshot is None:
                if args.do_print:
                    print("No GPU metrics collected", file=sys.stderr)
                if args.push:
                    print("No GPU metrics collected, skipping this cycle", file=sys.stderr)
                if health is not None and args.push:
                    health.record_failure("gpu_metrics_unavailable")
            else:
                payload = snapshot_to_public_dict(config, snapshot)
                if args.do_print:
                    print(json.dumps(payload, ensure_ascii=False))
                if args.push:
                    assert session is not None
                    if report(session, config, payload):
                        if health is not None:
                            health.record_success()
                    elif health is not None:
                        health.record_failure("report request failed")

            time.sleep(config["interval"])
    else:
        threading.Event().wait()


if __name__ == "__main__":
    main()
