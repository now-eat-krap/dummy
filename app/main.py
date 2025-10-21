import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Dict, List

import httpx
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .api import _parse_flux_csv, router as api_router
from .ba import _get_influx_config, router as ba_router

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
async def heatmap(request: Request) -> HTMLResponse:
    grid, max_count = await _build_heatmap_grid(request)
    context = {
        "request": request,
        "grid": grid,
        "cols": HEATMAP_COLS,
        "rows": HEATMAP_ROWS,
        "hours": HEATMAP_LOOKBACK_HOURS,
        "max_count": max_count,
    }
    return templates.TemplateResponse("heatmap.html", context)


async def _build_heatmap_grid(request: Request) -> tuple[list[list[float]], int]:
    raw_rows = await _fetch_heatmap_rows(request)
    grid = [[0 for _ in range(HEATMAP_COLS)] for _ in range(HEATMAP_ROWS)]
    max_count = 0
    for entry in raw_rows:
        x_value = entry.get("x_bin")
        y_value = entry.get("y_bin")
        count_value = entry.get("count") or entry.get("_value")
        try:
            x_idx = int(float(x_value))
            y_idx = int(float(y_value))
        except (TypeError, ValueError):
            continue
        if not (0 <= x_idx < HEATMAP_COLS and 0 <= y_idx < HEATMAP_ROWS):
            continue
        try:
            count = int(float(count_value))
        except (TypeError, ValueError):
            continue
        grid[y_idx][x_idx] += count
        if grid[y_idx][x_idx] > max_count:
            max_count = grid[y_idx][x_idx]

    if max_count > 0:
        for y in range(HEATMAP_ROWS):
            for x in range(HEATMAP_COLS):
                grid[y][x] = grid[y][x] / max_count
    else:
        for y in range(HEATMAP_ROWS):
            for x in range(HEATMAP_COLS):
                grid[y][x] = 0.0
    return grid, max_count


async def _fetch_heatmap_rows(request: Request) -> List[Dict[str, str]]:
    cfg = _get_influx_config()
    query = (
        f'from(bucket: "{cfg["bucket"]}")\n'
        f"  |> range(start: -{HEATMAP_LOOKBACK_HOURS}h)\n"
        '  |> filter(fn: (r) => r["_measurement"] == "logflow_click")\n'
        '  |> pivot(rowKey: ["_time", "site", "route", "section"], columnKey: ["_field"], valueColumn: "_value")\n'
        '  |> keep(columns: ["_time", "site", "route", "section", "count", "x_bin", "y_bin"])\n'
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
