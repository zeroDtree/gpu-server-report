# gpu-server-report

GPU metrics agent: `nvidia-smi` → optional local HTTP API → periodic POST to upstream report API.

## Run

```bash
uv sync && cp .env.example .env
uv run python reporter.py          # default: push-only every AGENT_REPORT_INTERVAL
```

Required for `--push`: `AGENT_SERVER_ID`, `AGENT_PSK`. Copy `.env.example` to `.env` and set production values before deploying.

## Configuration

| Variable | Role |
|----------|------|
| `REPORT_API_URL` | Report base URL (default `http://localhost:8080`) |
| `AGENT_SERVER_ID` | Sent as `serverId` |
| `AGENT_PSK` | `X-Agent-PSK` header value (must match server) |
| `AGENT_RESOURCE_LEVEL` | Optional `resourceLevel` |
| `AGENT_REPORT_INTERVAL` | Seconds (default `30`, min `5`) |
| `AGENT_HTTP_HOST` / `AGENT_HTTP_PORT` | `--serve` bind (default `0.0.0.0:9090`) |
| `AGENT_HEALTH_HOST` / `AGENT_HEALTH_PORT` | Health (default `127.0.0.1:9092`; `0` disables) |

## CLI

Combinable: `--print`, `--serve`, `--push`. No flags → `--push` only.

```bash
uv run python reporter.py --print
uv run python reporter.py --serve --push
```

## Metrics JSON

Shared by `--print`, `GET /metrics`, and `--push`:

```json
{
  "serverId": "gpu-node-01",
  "resourceLevel": "NVIDIA RTX A6000",
  "collectedAt": "2026-05-14T12:00:00+00:00",
  "gpus": [{ "index": 0, "name": "...", "avgUtil": 0.12, "memUsedMb": 8192, "memTotalMb": 49152 }],
  "summary": { "gpuCount": 1, "avgUtil": 0.12, "avgMemUsedMb": 8192 }
}
```

## Upstream report

- **URL:** `{REPORT_API_URL}/api/internal/servers/report`
- **Headers:** `X-Agent-Server-Id: <AGENT_SERVER_ID>`, `X-Agent-PSK: <AGENT_PSK>`
