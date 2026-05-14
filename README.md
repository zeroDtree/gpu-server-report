# gpu-server-report

Small **GPU metrics agent**: aggregates data from `nvidia-smi`, optionally exposes a local JSON HTTP API, and optionally **POST**s the **same JSON document** to an upstream report API on a fixed interval. The document includes **per-GPU** rows and a **summary** (averages).

- **Python:** 3.11+ ([pyproject.toml](pyproject.toml))
- **GPU host:** NVIDIA driver and `nvidia-smi` in `PATH` when collecting metrics
- **Network:** reachability to the upstream API when using `--push`; scrapers need access to the bind address when using `--serve`

## Quick start

```bash
uv sync
cp .env.example .env
# Edit .env (set AGENT_PSK when using default push mode)
uv run python reporter.py
```

With no CLI mode flags, the agent runs **push-only** (same as legacy): it posts to the configured backend every `AGENT_REPORT_INTERVAL` seconds.

## Configuration

Environment variables are loaded from `.env` (see [.env.example](.env.example)). CLI flags override HTTP bind values after env is applied.

| Variable                | Role                                                                                                                                                                                                                                                   |
| ----------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `REPORT_API_URL`        | Upstream report API base URL (default `http://localhost:8080`; trailing slash stripped).                                                                                                                                                               |
| `AGENT_PSK`             | Pre-shared key; **required when `--push` is enabled** (sent as `X-Agent-PSK`).                                                                                                                                                                         |
| `AGENT_HOSTNAME`        | Reported hostname (default: system hostname).                                                                                                                                                                                                          |
| `AGENT_RESOURCE_LEVEL`  | Optional. Free-form label sent as `resourceLevel` (for example the GPU product string). If unset or empty, uses the first line of `nvidia-smi --query-gpu=name --format=csv,noheader,nounits` unchanged. If that fails, uses `unknown` with a warning. |
| `AGENT_REPORT_INTERVAL` | Seconds between collection ticks when `--print` and/or `--push` (default `30`, minimum `5`).                                                                                                                                                           |
| `AGENT_HTTP_HOST`       | Default bind host for `--serve` (default `0.0.0.0`).                                                                                                                                                                                                   |
| `AGENT_HTTP_PORT`       | Default bind port for `--serve` (default `9090`; invalid or out-of-range values fall back with a warning).                                                                                                                                             |

**Multi-GPU nodes:** When `AGENT_RESOURCE_LEVEL` is omitted, only the **first** GPU name from `nvidia-smi` is used. Set the variable explicitly if you need a different label.

## CLI modes

Modes are **combinable**: `--print`, `--serve`, `--push`.

| Flag                         | Behavior                                                                                                                   |
| ---------------------------- | -------------------------------------------------------------------------------------------------------------------------- |
| `--print`                    | On each interval, print **one JSON line** to stdout — identical payload to `GET /metrics` and `--push` (errors to stderr). |
| `--serve`                    | Start a background HTTP server (`ThreadingHTTPServer`) for `GET /metrics` and `GET /health`.                               |
| `--push`                     | On each interval, `POST` JSON to the upstream report endpoint.                                                             |
| `--http-host`, `--http-port` | Override bind address/port for `--serve` (defaults from env, see table above).                                             |

If you pass **no** mode flags, the agent enables **`--push` only** (backward compatible).

Examples:

```bash
# Default: push only (requires AGENT_PSK)
uv run python reporter.py

# Print metrics on each interval (no upstream)
uv run python reporter.py --print

# Local HTTP API only (blocks until stopped)
uv run python reporter.py --serve --http-port 9090

# Scrape locally and push upstream
uv run python reporter.py --serve --push --print
```

When only `--serve` is used, there is no interval loop; the process stays up and serves HTTP until terminated.

## Unified metrics JSON

**`--print`**, **`GET /metrics`**, and **`--push`** use the **same** object shape (field names are camelCase).

**Breaking change:** Older clients expected only top-level `avgUtil` and `avgMemUsedMb`. Those averages now live under **`summary`**, and per-GPU data is under **`gpus`**.

| Field                  | Description                                                             |
| ---------------------- | ----------------------------------------------------------------------- |
| `hostname`             | From config / env.                                                      |
| `resourceLevel`        | From `AGENT_RESOURCE_LEVEL` or auto-detected GPU product name.          |
| `collectedAt`          | UTC ISO-8601 timestamp when the snapshot was built.                     |
| `gpus`                 | Array of one object per GPU from `nvidia-smi`.                          |
| `gpus[].index`         | GPU index from `nvidia-smi`.                                            |
| `gpus[].name`          | GPU product name.                                                       |
| `gpus[].avgUtil`       | GPU utilization **0.0–1.0** (from `utilization.gpu` / 100).             |
| `gpus[].memUsedMb`     | `memory.used` (MiB-style unit as reported by `nvidia-smi`).             |
| `gpus[].memTotalMb`    | `memory.total` (same unit).                                             |
| `summary.gpuCount`     | Number of GPUs in `gpus`.                                               |
| `summary.avgUtil`      | Mean of `gpus[].avgUtil`.                                               |
| `summary.avgMemUsedMb` | Integer mean of `gpus[].memUsedMb` (same rule as before: sum // count). |

Example (illustrative):

```json
{
  "hostname": "gpu-node-01",
  "resourceLevel": "NVIDIA RTX A6000",
  "collectedAt": "2026-05-14T12:00:00+00:00",
  "gpus": [
    {
      "index": 0,
      "name": "NVIDIA RTX A6000",
      "avgUtil": 0.12,
      "memUsedMb": 8192,
      "memTotalMb": 49152
    }
  ],
  "summary": {
    "gpuCount": 1,
    "avgUtil": 0.12,
    "avgMemUsedMb": 8192
  }
}
```

## HTTP API (`--serve`)

Each `GET /metrics` request runs the same `nvidia-smi` query again (fine for debugging; high scrape rates may contend with the push/print loop).

| Method / path  | Status | Body                                                                                  |
| -------------- | ------ | ------------------------------------------------------------------------------------- |
| `GET /health`  | 200    | `{"ok": true}` — does not query the GPU.                                              |
| `GET /metrics` | 200    | Unified metrics JSON (see [Unified metrics JSON](#unified-metrics-json)).             |
| `GET /metrics` | 503    | `{"ok": false, "error": "gpu_metrics_unavailable"}` when collection fails or no GPUs. |
| Other paths    | 404    | `{"ok": false, "error": "not_found"}`.                                                |

Example (default port `9090`; `-sS` = silent body, but show errors on stderr):

```bash
curl -sS http://127.0.0.1:9090/metrics
curl -sS http://127.0.0.1:9090/health
```

## Upstream report (`--push`)

- **URL:** `{REPORT_API_URL}/api/internal/servers/report`
- **Header:** `X-Agent-PSK: <AGENT_PSK>`
- **JSON body:** Same object as [Unified metrics JSON](#unified-metrics-json) (and the same as each `--print` line).

Failures are logged to stderr (`ERROR: report failed: ...`).