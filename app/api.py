import csv
import io
import logging
import os
import re
from typing import Any, Dict, List

import httpx
from fastapi import APIRouter, HTTPException, Query, Request

router = APIRouter(prefix="/api")
logger = logging.getLogger("uvicorn.error")
_BUCKET_RE = re.compile(r"^\d+[smhd]$")


def _get_influx_config() -> Dict[str, str]:
    return {
        "url": os.getenv("INFLUX_URL", "http://influxdb:8086").rstrip("/"),
        "token": os.getenv("INFLUX_TOKEN", "logflow-dev-token"),
        "org": os.getenv("INFLUX_ORG", "logflow"),
        "bucket": os.getenv("INFLUX_BUCKET", "logflow"),
    }


def _escape_flux(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _site_filter(site: str | None) -> str:
    if not site:
        return ""
    escaped = _escape_flux(site)
    return f'  |> filter(fn: (r) => r["site"] == "{escaped}")\n'


def _parse_flux_csv(text: str) -> List[Dict[str, str]]:
    rows: List[Dict[str, str]] = []
    reader = csv.reader(io.StringIO(text))
    header: List[str] = []
    for row in reader:
        if not row:
            continue
        first = row[0]
        if first.startswith("#"):
            continue
        if first == "result" and len(row) > 1 and row[1] == "table":
            header = row
            continue
        if not header:
            header = row
            continue
        if len(row) != len(header):
            continue
        rows.append(dict(zip(header, row)))
    return rows


async def _query_flux(request: Request, query: str) -> List[Dict[str, str]]:
    cfg = _get_influx_config()
    headers = {
        "Authorization": f"Token {cfg['token']}",
        "Content-Type": "application/vnd.flux",
        "Accept": "application/csv",
    }
    client: httpx.AsyncClient | None = getattr(request.app.state, "http_client", None)
    try:
        if client is None:
            async with httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=3.0)) as temp_client:
                response = await temp_client.post(
                    f"{cfg['url']}/api/v2/query",
                    params={"org": cfg["org"]},
                    headers=headers,
                    content=query.encode("utf-8"),
                )
        else:
            response = await client.post(
                f"{cfg['url']}/api/v2/query",
                params={"org": cfg["org"]},
                headers=headers,
                content=query.encode("utf-8"),
            )
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("Influx query request failed: %s", exc)
        raise HTTPException(status_code=502, detail="Influx query failed") from exc

    if response.status_code >= 400:
        logger.warning("Influx query error %s: %s", response.status_code, response.text)
        raise HTTPException(status_code=502, detail="Influx query rejected")

    return _parse_flux_csv(response.text)


@router.get("/summary")
async def summary(
    request: Request,
    hours: int = Query(24, ge=1, le=168),
    site: str | None = Query(None),
) -> Dict[str, Any]:
    cfg = _get_influx_config()
    query = (
        f'from(bucket: "{_escape_flux(cfg["bucket"])}")\n'
        f"  |> range(start: -{hours}h)\n"
        '  |> filter(fn: (r) => r._measurement == "logflow" and r._field == "count")\n'
        f"{_site_filter(site)}"
        "  |> group(columns: [])\n"
        "  |> sum()\n"
    )
    rows = await _query_flux(request, query)
    total = 0
    for row in rows:
        value = row.get("_value")
        if value is None:
            continue
        try:
            total += int(float(value))
        except ValueError:
            continue
    return {"site": site, "hours": hours, "count": total}


@router.get("/top-routes")
async def top_routes(
    request: Request,
    hours: int = Query(24, ge=1, le=168),
    limit: int = Query(10, ge=1, le=50),
    site: str | None = Query(None),
) -> Dict[str, Any]:
    cfg = _get_influx_config()
    query = (
        f'from(bucket: "{_escape_flux(cfg["bucket"])}")\n'
        f"  |> range(start: -{hours}h)\n"
        '  |> filter(fn: (r) => r._measurement == "logflow" and r._field == "count")\n'
        '  |> filter(fn: (r) => exists r.t and r.t == "page")\n'
        f"{_site_filter(site)}"
        '  |> group(columns: ["route"])\n'
        '  |> sum(column: "_value")\n'
        '  |> sort(columns: ["_value"], desc: true)\n'
        f"  |> limit(n: {limit})\n"
    )
    rows = await _query_flux(request, query)
    totals: Dict[str, int] = {}
    for row in rows:
        route = row.get("route")
        value = row.get("_value")
        if not route or value is None:
            continue
        try:
            totals[route] = totals.get(route, 0) + int(float(value))
        except ValueError:
            continue
    sorted_routes = sorted(totals.items(), key=lambda item: item[1], reverse=True)[:limit]
    return {
        "site": site,
        "hours": hours,
        "limit": limit,
        "routes": [{"route": route, "count": count} for route, count in sorted_routes],
    }


@router.get("/series")
async def series(
    request: Request,
    hours: int = Query(24, ge=1, le=168),
    bucket: str = Query("5m"),
    site: str | None = Query(None),
) -> Dict[str, Any]:
    if not _BUCKET_RE.match(bucket):
        raise HTTPException(status_code=400, detail="Invalid bucket size")
    cfg = _get_influx_config()
    query = (
        f'from(bucket: "{_escape_flux(cfg["bucket"])}")\n'
        f"  |> range(start: -{hours}h)\n"
        '  |> filter(fn: (r) => r._measurement == "logflow" and r._field == "count")\n'
        f"{_site_filter(site)}"
        f"  |> aggregateWindow(every: {bucket}, fn: sum, createEmpty: false)\n"
        "  |> group(columns: [])\n"
    )
    rows = await _query_flux(request, query)
    points: List[Dict[str, Any]] = []
    for row in rows:
        ts = row.get("_time")
        value = row.get("_value")
        if ts is None or value is None:
            continue
        try:
            count = int(float(value))
        except ValueError:
            continue
        points.append({"ts": ts, "count": count})
    points.sort(key=lambda item: item["ts"])
    return {"site": site, "hours": hours, "bucket": bucket, "points": points}
