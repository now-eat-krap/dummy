# Logflow Lightweight Analytics

Fast, private, and resource-friendly analytics for local environments, powered by FastAPI, InfluxDB v2, and a tiny vanilla JS snippet.

## Features

- One-line snapshot beacon that asks the server worker to capture a WebP background for the current route.
- FastAPI collector translates JSON payloads into InfluxDB line protocol with route normalization to keep series cardinality low.
- Zero-token exposure: dashboard consumes FastAPI proxy endpoints for Flux queries, protecting the Influx admin token.
- Static HTML dashboard shows total events, top routes, and a recent event timeline (type, element, coordinates) without external dependencies.
- Server-side Puppeteer worker salvages full-page renders; browsers never ship HTML payloads or layout skeletons anymore.
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
        data-endpoint="http://localhost:9000/snapshot/request"
        data-site="logflow"></script>
```

- `data-site`: optional site identifier stored alongside the snapshot metadata.
- `data-endpoint`: override if the FastAPI host lives behind a proxy; defaults to `/snapshot/request`.
- `data-snapshot`: optional cache bucket (defaults to `default`).
- `data-vp`, `data-grid`, `data-section`: optional identifiers to align with heatmap filters.
- The snippet deduplicates per route/site/snapshot per day using `localStorage` so the worker is pinged only once daily per combination.

## Automatic Snapshots

- The snippet fires a tiny JSON beacon (`url`, `site`, `route`, optional filters) to `/snapshot/request`.
- FastAPI resolves the cache path, calls the local Puppeteer worker (`snapshot-worker`) and waits for the capture to finish.
- The worker launches headless Chromium, renders the page at the requested viewport, and saves a WebP to `heatmap_cache/<snapshot>/<route>/<viewport>/<grid>/<section>/snapshot.webp` with a matching `meta.json`.
- Heatmap views load the WebP instead of the old layout skeleton, so backgrounds match the live UI precisely without browsers sharing DOM structure.
- Snapshots are deduplicated per day; clear the `heatmap_cache` entry or the `logflow:snapshot:*` keys in `localStorage` if you need a fresh capture immediately.

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
- Fields: `count` (int), `depth` (int), `sec` (int), `vp_w`, `vp_h`, `vp_dpr`, `path` (string), `element` (string, optional), `cx`/`cy` (ints, optional), `payload` (stringified JSON summary)

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
- Snapshot requests only include the current URL and identifiers; the Puppeteer worker does the rendering on the server so markup never leaves the browser.
- `/ba` always responds `204` to avoid impacting UX even on failures.
- Consider rotating the admin token for production or using scoped tokens.

## Troubleshooting

- **Port 9000 in use**: stop the conflicting service or change the published port in `docker/docker-compose.yml`.
- **Docker missing**: install Docker Desktop or Docker Engine + Compose plugin.
- **Health check fails**: inspect logs with `docker compose -f docker/docker-compose.yml logs app`.
- **Influx UI access**: the database isn’t exposed; exec into the container (`docker compose exec influxdb influx`) if you need CLI access.
- **Proxy setups**: ensure your dev server rewrites `/ba` exactly to `http://localhost:9000/ba` so cookies and auth headers don’t leak.
