import hashlib
import json
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import urlencode

import httpx
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .api import _escape_flux, _parse_flux_csv, router as api_router
from .ba import _get_influx_config, _normalize_route, router as ba_router
from .cache_utils import HEATMAP_CACHE_DIR, load_metadata, snapshot_cache_path
from .snapshot import router as snapshot_router

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
app.include_router(snapshot_router)


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

    snapshot_info = _snapshot_media_info(
        route_norm=route_norm,
        snapshot_hash=cache_snapshot_key,
        vp_bucket=cache_vp_key,
        grid_id=grid_id,
        section=cache_section_key,
    )
    cache_path = snapshot_info["path"]
    etag = snapshot_info["etag"]
    metadata = snapshot_info["metadata"]
    snapshot_media_url = None
    if snapshot_info["available"]:
        snapshot_media_url = _build_snapshot_media_url(
            route_norm=route_norm,
            snapshot_hash=cache_snapshot_key,
            vp_bucket=cache_vp_key,
            grid_id=grid_id,
            section=cache_section_key,
            etag=etag,
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
        "snapshot_media_url": snapshot_media_url,
        "snapshot_available": snapshot_info["available"],
        "snapshot_meta": metadata,
        "snapshot_size": _format_filesize(snapshot_info["size_bytes"]),
        "snapshot_aspect_ratio": _format_aspect_ratio(metadata),
        "snapshot_captured": _format_timestamp(metadata.get("captured_at")) if metadata else "",
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
                "width": entry.get("width"),
                "height": entry.get("height"),
                "bytes": entry.get("bytes"),
                "format": entry.get("format") or "webp",
                "vp_bucket": entry.get("vp_bucket"),
                "grid_id": entry.get("grid_id"),
                "section": entry.get("section"),
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
                "resolution": _format_resolution(item.get("width"), item.get("height")),
                "size": _format_filesize(item.get("bytes")),
                "format": str(item.get("format") or "webp").upper(),
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


@app.get("/heatmap/media")
async def heatmap_media(
    route: str = Query("/", description="Route path to inspect"),
    snapshot: str = Query("default", description="Snapshot hash identifier"),
    vp: str = Query("any", description="Viewport bucket identifier"),
    grid: Optional[str] = Query(None, description="Grid identifier, e.g. 12x8"),
    section: Optional[str] = Query(None, description="Section label"),
) -> FileResponse:
    route_norm = _normalize_route(route or "/")
    _, _, grid_id = _parse_grid_identifier(grid, HEATMAP_COLS, HEATMAP_ROWS)
    snapshot_hash = (snapshot or "default").strip() or "default"
    vp_bucket = (vp or "any").strip() or "any"
    section_value = (section or "all").strip() or "all"

    info = _snapshot_media_info(
        route_norm=route_norm,
        snapshot_hash=snapshot_hash,
        vp_bucket=vp_bucket,
        grid_id=grid_id,
        section=section_value,
    )
    if not info["available"]:
        raise HTTPException(status_code=404, detail="Snapshot not found")

    headers: Dict[str, str] = {}
    if info["etag"]:
        headers["ETag"] = info["etag"]
    media_type = _guess_media_type(info["metadata"].get("format"))
    return FileResponse(info["path"], media_type=media_type, headers=headers)


def _snapshot_media_info(
    *,
    route_norm: str,
    snapshot_hash: str,
    vp_bucket: str,
    grid_id: str,
    section: str,
) -> Dict[str, Any]:
    cache_path = snapshot_cache_path(route_norm, snapshot_hash, vp_bucket, grid_id, section)
    metadata = _read_snapshot_metadata(cache_path)
    available = cache_path.exists()
    etag = metadata.get("sha256")
    size_bytes = 0
    if available:
        try:
            size_bytes = cache_path.stat().st_size
        except OSError:
            size_bytes = 0
        if not etag:
            try:
                with cache_path.open("rb") as file_obj:
                    etag = hashlib.sha256(file_obj.read()).hexdigest()
            except OSError:
                etag = None
    try:
        relative = cache_path.relative_to(HEATMAP_CACHE_DIR)
    except ValueError:
        relative = cache_path
    metadata.setdefault("format", "webp")
    metadata.setdefault("rel_path", str(relative).replace("\\", "/"))
    return {
        "path": cache_path,
        "relative_path": relative,
        "available": available,
        "etag": etag,
        "size_bytes": size_bytes,
        "metadata": metadata,
    }


def _read_snapshot_metadata(cache_path: Path) -> Dict[str, Any]:
    meta_path = cache_path.with_name("meta.json")
    if not meta_path.exists():
        return {}
    try:
        return json.loads(meta_path.read_text(encoding="utf-8"))
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("Failed to read snapshot metadata %s: %s", meta_path, exc)
        return {}


def _build_snapshot_media_url(
    *,
    route_norm: str,
    snapshot_hash: str,
    vp_bucket: str,
    grid_id: str,
    section: str,
    etag: Optional[str],
) -> str:
    params: List[Tuple[str, str]] = [
        ("route", route_norm or "/"),
        ("snapshot", snapshot_hash or "default"),
        ("vp", vp_bucket or "any"),
        ("grid", grid_id or ""),
        ("section", section or ""),
    ]
    if etag:
        params.append(("v", etag[:12]))
    query = urlencode([(key, value) for key, value in params if value])
    return f"/heatmap/media?{query}"


def _format_filesize(value: Any) -> str:
    try:
        size = int(value)
    except (TypeError, ValueError):
        return ""
    if size <= 0:
        return ""
    units = ["B", "KB", "MB", "GB", "TB"]
    number = float(size)
    idx = 0
    while number >= 1024 and idx < len(units) - 1:
        number /= 1024.0
        idx += 1
    if idx == 0:
        return f"{int(number)} {units[idx]}"
    return f"{number:.1f} {units[idx]}"


def _format_aspect_ratio(metadata: Dict[str, Any]) -> Optional[str]:
    try:
        width = int(metadata.get("width") or 0)
        height = int(metadata.get("height") or 0)
    except (TypeError, ValueError):
        return None
    if width <= 0 or height <= 0:
        return None
    return f"{width} / {height}"


def _format_resolution(width: Any, height: Any) -> str:
    try:
        w = int(width)
        h = int(height)
    except (TypeError, ValueError):
        return ""
    if w <= 0 or h <= 0:
        return ""
    return f"{w}×{h}"


def _guess_media_type(format_hint: Any) -> str:
    token = str(format_hint or "webp").lower().strip()
    if token in {"jpeg", "jpg"}:
        return "image/jpeg"
    if token == "png":
        return "image/png"
    if token == "avif":
        return "image/avif"
    return "image/webp"
