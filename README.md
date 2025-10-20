# Logflow Lightweight Analytics

Fast, private, and resource-friendly analytics for local environments, powered by FastAPI, InfluxDB v2, and a tiny vanilla JS snippet.

## Features

- Drop-in snippet that tracks page, click, scroll, SPA transition, and heartbeat events with sampling, UID/SID storage, and sendBeacon fallback.
- FastAPI collector translates JSON payloads into InfluxDB line protocol with route normalization to keep series cardinality low.
- Zero-token exposure: dashboard consumes FastAPI proxy endpoints for Flux queries, protecting the Influx admin token.
- Static HTML dashboard shows total events, top routes, and a lightweight time series without external dependencies.
- Docker Compose stack brings up InfluxDB 2.x and the app with conservative CPU/memory limits and no public Influx port.

## Quick Start

```bash
curl -fsSL https://raw.githubusercontent.com/your-org/logflow/main/scripts/install.sh | bash
```

Or manually:

```bash
git clone https://github.com/your-org/logflow.git
cd logflow
scripts/install.sh
```

The stack exposes the dashboard at `http://localhost:9000/`.

To tear down everything (including volumes) run:

```bash
scripts/uninstall.sh
```

## Embed Snippet

```html
<script src="http://localhost:9000/ba.js"
        data-site="logflow"
        data-endpoint="http://localhost:9000/ba"
        data-click="true" data-scroll="true" data-spa="true" data-hb="15"
        data-sample="1.0"
        defer></script>
```

- `data-sample`: 0–1 float for sampling.
- `data-click`, `data-scroll`, `data-spa`: enable optional collectors.
- `data-hb`: heartbeat cadence in seconds (0 disables).

For HTTPS dev setups (Next.js, Vite, etc.) proxy `/ba` to `http://localhost:9000/ba` so the snippet can POST over HTTP without mixed-content issues.

## Configuration

Environment defaults (override via `.env` or Compose environment):

| Variable        | Default                 | Purpose                          |
|----------------|-------------------------|----------------------------------|
| `INFLUX_URL`   | `http://influxdb:8086`  | Internal InfluxDB address        |
| `INFLUX_TOKEN` | `logflow-dev-token`     | Admin token for writes/queries   |
| `INFLUX_ORG`   | `logflow`               | Influx organization              |
| `INFLUX_BUCKET`| `logflow`               | Target bucket                    |
| `ALLOW_ORIGINS`| `*`                     | CORS origins for FastAPI         |

Duplicate the provided `.env.example` if you need to override values.

## Data Model

Measurement: `logflow`

- Tags: `site`, `t` (event type), `route`
- Fields: `count` (int), `depth` (int), `sec` (int), `vp_w`, `vp_h`, `vp_dpr`, `path` (string)

Routes are normalized on ingest (`/\d+` and long hex segments → `/:id`) to prevent exploding tag cardinality.

## Flux Examples

Total events last 6 hours:

```flux
from(bucket: "logflow")
  |> range(start: -6h)
  |> filter(fn: (r) => r._measurement == "logflow" and r._field == "count")
  |> group(columns: [])
  |> sum()
```

Top page routes:

```flux
from(bucket: "logflow")
  |> range(start: -24h)
  |> filter(fn: (r) => r._measurement == "logflow" and r._field == "count")
  |> filter(fn: (r) => r.t == "page")
  |> group(columns: ["route"])
  |> sum()
  |> sort(columns: ["_value"], desc: true)
  |> limit(n: 10)
```

Series aggregation:

```flux
from(bucket: "logflow")
  |> range(start: -24h)
  |> filter(fn: (r) => r._measurement == "logflow" and r._field == "count")
  |> aggregateWindow(every: 5m, fn: sum, createEmpty: false)
  |> group(columns: [])
```

## Resource & Privacy Notes

- InfluxDB container is private to the Compose network (no published ports).
- App container limited to 0.5 CPU / 256 MiB; Influx limited to 0.8 CPU / 512 MiB.
- No PII collection: snippet transmits route/title/ref/url only.
- `/ba` always responds `204` to avoid impacting UX even on failures.
- Consider rotating the admin token for production or using scoped tokens.

## Troubleshooting

- **Port 9000 in use**: stop the conflicting service or change the published port in `docker/docker-compose.yml`.
- **Docker missing**: install Docker Desktop or Docker Engine + Compose plugin.
- **Health check fails**: inspect logs with `docker compose -f docker/docker-compose.yml logs app`.
- **Influx UI access**: the database isn’t exposed; exec into the container (`docker compose exec influxdb influx`) if you need CLI access.
- **Proxy setups**: ensure your dev server rewrites `/ba` exactly to `http://localhost:9000/ba` so cookies and auth headers don’t leak.
