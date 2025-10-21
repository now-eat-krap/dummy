import hashlib
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime
from html import escape
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import urlencode

import httpx
from fastapi import FastAPI, Request, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .api import _escape_flux, _parse_flux_csv, router as api_router
from .ba import _get_influx_config, _normalize_route, router as ba_router
from .cache_utils import HEATMAP_CACHE_DIR, load_metadata, snapshot_cache_path

BASE_DIR = Path(__file__).resolve().parent
BA_JS_PATH = BASE_DIR / "static" / "ba.js"
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
logger = logging.getLogger("uvicorn.error")


def _parse_origins(raw: str) -> List[str]:
    raw = raw.strip()
    if raw == "*":
        return ["*"]
    values = [origin.strip() for origin in raw.split(",")]
    return [origin for origin in values if origin]


def _parse_bool_env(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    lowered = value.strip().lower()
    return lowered in {"1", "true", "yes", "on"}


def _parse_int_env(name: str, default: int, minimum: int = 1) -> int:
    value = os.getenv(name)
    if value is None:
        return max(default, minimum)
    try:
        parsed = int(value)
    except ValueError:
        return max(default, minimum)
    return parsed if parsed >= minimum else minimum


@asynccontextmanager
async def lifespan(app: FastAPI):
    client = httpx.AsyncClient(timeout=httpx.Timeout(5.0, connect=2.0))
    app.state.http_client = client
    try:
        yield
    finally:
        await client.aclose()


app = FastAPI(lifespan=lifespan)

allow_origins = _parse_origins(os.getenv("ALLOW_ORIGINS", "*"))
allow_origin_regex = None
if allow_origins == ["*"]:
    # Use regex fallback so FastAPI echoes the caller origin instead of "*".
    allow_origin_regex = ".*"
    allow_origins = []
allow_credentials = _parse_bool_env("ALLOW_CREDENTIALS", default=True)
app.add_middleware(
    CORSMiddleware,
    allow_origins=allow_origins,
    allow_origin_regex=allow_origin_regex,
    allow_credentials=allow_credentials,
    allow_methods=["*"],
    allow_headers=["*"],
)

HEATMAP_LOOKBACK_HOURS = _parse_int_env("HEATMAP_LOOKBACK_HOURS", default=24, minimum=1)
HEATMAP_COLS = _parse_int_env("HEATMAP_COLS", default=12, minimum=1)
HEATMAP_ROWS = _parse_int_env("HEATMAP_ROWS", default=8, minimum=1)

app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
app.include_router(ba_router)
app.include_router(api_router)


@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    return templates.TemplateResponse("index.html", {"request": request})


@app.get("/ba.js")
async def snippet() -> FileResponse:
    return FileResponse(BA_JS_PATH, media_type="application/javascript")


@app.get("/heatmap", response_class=HTMLResponse)
async def heatmap(
    request: Request,
    route: str = Query("/", description="Route path to inspect"),
    snapshot: str = Query("default", description="Snapshot hash identifier"),
    vp: str = Query("", description="Viewport bucket identifier"),
    grid: Optional[str] = Query(None, description="Grid identifier, e.g. 12x8"),
    section: Optional[str] = Query(None, description="Section label"),
    site: Optional[str] = Query(None, description="Site identifier"),
    hours: int = Query(HEATMAP_LOOKBACK_HOURS, ge=1, le=168, description="Lookback window in hours"),
) -> HTMLResponse:
    route_norm = _normalize_route(route or "/")
    cols, rows, grid_id = _parse_grid_identifier(grid, HEATMAP_COLS, HEATMAP_ROWS)

    snapshot_param = (snapshot or "").strip()
    snapshot_hash = snapshot_param or "default"
    snapshot_filter = None if snapshot_hash.lower() in {"*", "all", "any"} else snapshot_hash

    vp_param = (vp or "").strip()
    vp_filter = None if vp_param.lower() in {"", "*", "any"} else vp_param

    section_param = (section or "").strip()
    section_filter = None if section_param.lower() in {"", "*", "all", "__all__"} else section_param

    site_param = (site or "").strip()

    heatmap_data = await _build_heatmap_grid(
        request,
        hours=hours,
        site=site_param or None,
        route_norm=route_norm,
        snapshot_hash=snapshot_filter,
        vp_bucket=vp_filter,
        grid_id=grid_id,
        cols=cols,
        rows=rows,
        section=section_filter,
    )

    cached_routes = _build_cached_route_links(
        snapshot_hash=snapshot_hash,
        route_norm=route_norm,
        current_route=route or route_norm,
        snapshot_param=snapshot_param,
        vp_param=vp_param,
        grid_param=grid,
        section_param=section_param,
        site_param=site_param,
        hours=hours,
    )

    cache_snapshot_key = snapshot_hash if snapshot_filter else (snapshot_hash or "all")
    cache_vp_key = vp_param if vp_filter else "any"
    cache_section_key = section_param if section_filter else "all"

    snapshot_html, etag, cache_path = _load_snapshot_html(
        route_norm=route_norm,
        snapshot_hash=cache_snapshot_key,
        vp_bucket=cache_vp_key,
        grid_id=grid_id,
        section=cache_section_key,
    )

    try:
        cache_rel = cache_path.relative_to(HEATMAP_CACHE_DIR)
    except ValueError:
        cache_rel = cache_path

    context = {
        "request": request,
        "grid": heatmap_data["grid"],
        "raw_grid": heatmap_data["raw"],
        "cells": heatmap_data["cells"],
        "cols": cols,
        "rows": rows,
        "hours": hours,
        "max_count": heatmap_data["max_count"],
        "total_count": heatmap_data["total_count"],
        "route_norm": route_norm,
        "snapshot_hash": snapshot_hash,
        "vp_bucket": cache_vp_key,
        "grid_id": grid_id,
        "section": section_param or "",
        "site": site_param,
        "snapshot_html": snapshot_html,
        "snapshot_available": etag is not None,
        "cache_path": str(cache_rel),
        "cache_full_path": str(cache_path),
        "cached_routes": cached_routes,
        "filters": {
            "site": site_param or "",
            "route": route_norm,
            "snapshot": snapshot_hash,
            "vp": vp_param,
            "grid": grid_id,
            "section": section_param or "",
            "hours": hours,
        },
    }
    response = templates.TemplateResponse("heatmap.html", context)
    if etag:
        response.headers["ETag"] = etag
    return response


def _build_cached_route_links(
    *,
    snapshot_hash: str,
    route_norm: str,
    current_route: str,
    snapshot_param: str,
    vp_param: str,
    grid_param: Optional[str],
    section_param: str,
    site_param: str,
    hours: int,
) -> List[Dict[str, Any]]:
    if not snapshot_hash:
        snapshot_hash = "default"
    entries = load_metadata(snapshot_hash)
    if not entries:
        return []
    filtered: List[Dict[str, Any]] = []
    for entry in entries:
        if site_param and entry.get("site") and entry.get("site") != site_param:
            continue
        route_value = entry.get("route") or entry.get("route_norm")
        if not route_value:
            continue
        norm_value = entry.get("route_norm") or _normalize_route(route_value)
        filtered.append(
            {
                "route": route_value,
                "route_norm": norm_value,
                "captured_at": entry.get("captured_at"),
                "boxes": entry.get("boxes"),
            }
        )
    if not filtered:
        return []
    filtered.sort(key=lambda item: item.get("captured_at") or 0, reverse=True)
    seen: Set[str] = set()
    links: List[Dict[str, Any]] = []
    for item in filtered:
        route_value = item["route"]
        norm_value = item["route_norm"]
        key = norm_value or route_value
        if key in seen:
            continue
        seen.add(key)
        url = _build_heatmap_link(
            route_value=route_value,
            snapshot_param=snapshot_param,
            vp_param=vp_param,
            grid_param=grid_param,
            section_param=section_param,
            site_param=site_param,
            hours=hours,
        )
        links.append(
            {
                "route": route_value or norm_value,
                "route_norm": norm_value,
                "url": url,
                "boxes": item.get("boxes") or 0,
                "captured_at": _format_timestamp(item.get("captured_at")),
                "active": norm_value == route_norm,
            }
        )
        if len(links) >= 16:
            break
    return links


def _build_heatmap_link(
    *,
    route_value: str,
    snapshot_param: str,
    vp_param: str,
    grid_param: Optional[str],
    section_param: str,
    site_param: str,
    hours: int,
) -> str:
    params: List[Tuple[str, str]] = []
    params.append(("route", route_value or "/"))
    if snapshot_param:
        params.append(("snapshot", snapshot_param))
    if vp_param:
        params.append(("vp", vp_param))
    if grid_param:
        params.append(("grid", grid_param))
    if section_param:
        params.append(("section", section_param))
    if site_param:
        params.append(("site", site_param))
    if hours:
        params.append(("hours", str(hours)))
    query = urlencode(params)
    return f"/heatmap?{query}"


def _format_timestamp(value: Any) -> str:
    try:
        timestamp = float(value)
    except (TypeError, ValueError):
        return ""
    if timestamp > 10_000_000_000:
        timestamp = timestamp / 1000.0
    try:
        dt = datetime.fromtimestamp(timestamp)
    except (OSError, ValueError):
        return ""
    return dt.strftime("%Y-%m-%d %H:%M")


async def _build_heatmap_grid(
    request: Request,
    *,
    hours: int,
    site: Optional[str],
    route_norm: str,
    snapshot_hash: Optional[str],
    vp_bucket: Optional[str],
    grid_id: str,
    cols: int,
    rows: int,
    section: Optional[str],
) -> Dict[str, Any]:
    raw_rows = await _fetch_heatmap_rows(
        request,
        hours=hours,
        site=site,
        route_norm=route_norm,
        snapshot_hash=snapshot_hash,
        vp_bucket=vp_bucket,
        grid_id=grid_id,
        section=section,
    )
    raw_grid = [[0 for _ in range(cols)] for _ in range(rows)]
    max_count = 0
    total_count = 0

    for entry in raw_rows:
        x_value = entry.get("x_bin")
        y_value = entry.get("y_bin")
        count_value = entry.get("count") or entry.get("_value")
        try:
            x_idx = int(float(x_value))
            y_idx = int(float(y_value))
            count = int(float(count_value))
        except (TypeError, ValueError):
            continue
        if not (0 <= x_idx < cols and 0 <= y_idx < rows):
            continue
        raw_grid[y_idx][x_idx] += count
        total_count += count
        if raw_grid[y_idx][x_idx] > max_count:
            max_count = raw_grid[y_idx][x_idx]

    if max_count > 0:
        normalized_grid = [
            [raw_grid[y][x] / max_count for x in range(cols)] for y in range(rows)
        ]
    else:
        normalized_grid = [[0.0 for _ in range(cols)] for _ in range(rows)]

    cells: List[Dict[str, float]] = []
    for y in range(rows):
        for x in range(cols):
            count = raw_grid[y][x]
            alpha = (count / max_count) if max_count else 0.0
            cells.append(
                {
                    "x": x,
                    "y": y,
                    "count": count,
                    "alpha": round(alpha, 4) if alpha else 0.0,
                }
            )

    return {
        "grid": normalized_grid,
        "raw": raw_grid,
        "max_count": max_count,
        "total_count": total_count,
        "cells": cells,
    }


async def _fetch_heatmap_rows(
    request: Request,
    *,
    hours: int,
    site: Optional[str],
    route_norm: str,
    snapshot_hash: Optional[str],
    vp_bucket: Optional[str],
    grid_id: str,
    section: Optional[str],
) -> List[Dict[str, str]]:
    cfg = _get_influx_config()
    filters = ['  |> filter(fn: (r) => r["_measurement"] == "logflow_click")']
    if site:
        filters.append(f'  |> filter(fn: (r) => r["site"] == "{_escape_flux(site)}")')
    if route_norm:
        filters.append(f'  |> filter(fn: (r) => r["route_norm"] == "{_escape_flux(route_norm)}")')
    if snapshot_hash:
        filters.append(f'  |> filter(fn: (r) => r["snapshot"] == "{_escape_flux(snapshot_hash)}")')
    if grid_id:
        filters.append(f'  |> filter(fn: (r) => r["grid"] == "{_escape_flux(grid_id)}")')
    if vp_bucket:
        filters.append(f'  |> filter(fn: (r) => r["vp"] == "{_escape_flux(vp_bucket)}")')
    if section:
        filters.append(f'  |> filter(fn: (r) => r["section"] == "{_escape_flux(section)}")')

    filters_str = "\n".join(filters)
    query = (
        f'from(bucket: "{_escape_flux(cfg["bucket"])}")\n'
        f"  |> range(start: -{hours}h)\n"
        f"{filters_str}\n"
        '  |> pivot(rowKey: ["_time", "site", "route", "route_norm", "section", "snapshot", "grid", "vp"], columnKey: ["_field"], valueColumn: "_value")\n'
        '  |> keep(columns: ["_time", "site", "route", "route_norm", "section", "snapshot", "grid", "vp", "count", "x_bin", "y_bin"])\n'
        '  |> group(columns: ["x_bin", "y_bin"])\n'
        '  |> sum(column: "count")\n'
    )
    rows = await _query_flux(request, query, cfg)
    return rows


async def _query_flux(request: Request, query: str, cfg: Dict[str, str]) -> List[Dict[str, str]]:
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
        logger.warning("Heatmap query request failed: %s", exc)
        return []

    if response.status_code >= 400:
        logger.warning("Heatmap query error %s: %s", response.status_code, response.text)
        return []

    return _parse_flux_csv(response.text)


def _parse_grid_identifier(grid_id: Optional[str], default_cols: int, default_rows: int) -> Tuple[int, int, str]:
    if grid_id:
        token = grid_id.lower().replace(" ", "").replace("*", "x")
        parts = token.split("x")
        if len(parts) == 2:
            try:
                cols = max(1, int(parts[0]))
                rows = max(1, int(parts[1]))
                return cols, rows, f"{cols}x{rows}"
            except ValueError:
                pass
    return default_cols, default_rows, f"{default_cols}x{default_rows}"


def _load_snapshot_html(
    route_norm: str,
    snapshot_hash: str,
    vp_bucket: str,
    grid_id: str,
    section: str,
) -> Tuple[str, Optional[str], Path]:
    cache_path = snapshot_cache_path(route_norm, snapshot_hash, vp_bucket, grid_id, section)
    if cache_path.exists():
        content = cache_path.read_text(encoding="utf-8")
        etag = hashlib.sha256(content.encode("utf-8")).hexdigest()
        return content, etag, cache_path
    placeholder = _fallback_snapshot_html(route_norm, snapshot_hash, vp_bucket, grid_id, section, cache_path)
    return placeholder, None, cache_path


def _fallback_snapshot_html(
    route_norm: str,
    snapshot_hash: str,
    vp_bucket: str,
    grid_id: str,
    section: str,
    expected_path: Path,
) -> str:
    safe_route = escape(route_norm or "/")
    safe_snapshot = escape(snapshot_hash or "default")
    safe_vp = escape(vp_bucket or "any")
    safe_grid = escape(grid_id or "")
    safe_section = escape(section or "all")
    safe_path = escape(str(expected_path))
    return (
        '<div class="heatmap-placeholder">'
        f'<p>No cached snapshot available for <code>{safe_route}</code>.</p>'
        f'<p>Expected cache file: <code>{safe_path}</code></p>'
        "<p>Visit the page with the tracking snippet to capture a snapshot automatically.</p>"
        f'<p>Key: snapshot={safe_snapshot}, viewport={safe_vp}, grid={safe_grid}, section={safe_section}</p>'
        "</div>"
    )
