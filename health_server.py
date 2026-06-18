"""Lightweight localhost HTTP health endpoint for GSAD host agents."""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlparse


@dataclass
class HealthState:
    agent: str
    hostname: str
    last_ok: bool = True
    last_error: str | None = None
    last_event_at: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def record_success(self, **extra: Any) -> None:
        with self._lock:
            self.last_ok = True
            self.last_error = None
            self.last_event_at = datetime.now(UTC).isoformat()
            self.extra.update(extra)

    def record_failure(self, message: str, **extra: Any) -> None:
        with self._lock:
            self.last_ok = False
            self.last_error = message
            self.last_event_at = datetime.now(UTC).isoformat()
            self.extra.update(extra)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            body: dict[str, Any] = {
                "ok": self.last_ok,
                "agent": self.agent,
                "hostname": self.hostname,
                "lastError": self.last_error,
            }
            if self.agent == "account-provisioner":
                body["lastPollAt"] = self.last_event_at
                body["lastPollOk"] = self.last_ok
            elif self.agent == "gpu-server-report":
                body["lastReportAt"] = self.last_event_at
                body["lastReportOk"] = self.last_ok
            else:
                body["lastEventAt"] = self.last_event_at
            body.update(self.extra)
            return body


def load_health_bind(*, default_port: int = 9091) -> tuple[str, int] | None:
    """Return (host, port) or None when health server is disabled."""
    raw_port = os.getenv("AGENT_HEALTH_PORT", str(default_port)).strip()
    if raw_port in {"", "0"}:
        return None
    try:
        port = int(raw_port)
    except ValueError:
        port = default_port
    if not (1 <= port <= 65535):
        return None
    host = os.getenv("AGENT_HEALTH_HOST", "127.0.0.1").strip() or "127.0.0.1"
    return host, port


def start_health_server(state: HealthState, host: str, port: int) -> None:
    """Start a daemon thread serving GET /health from *state*."""

    class HealthHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            del format, args

        def do_GET(self) -> None:
            path = urlparse(self.path).path
            if path != "/health":
                payload = json.dumps({"ok": False, "error": "not_found"}).encode("utf-8")
                self.send_response(404)
            else:
                payload = json.dumps(state.snapshot()).encode("utf-8")
                self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    server = ThreadingHTTPServer((host, port), HealthHandler)
    thread = threading.Thread(
        target=server.serve_forever,
        name=f"health-{state.agent}",
        daemon=True,
    )
    thread.start()
    print(f"Health server — http://{host}:{port}/health", flush=True)
