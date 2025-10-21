import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import List

import httpx
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .api import router as api_router
from .ba import router as ba_router

BASE_DIR = Path(__file__).resolve().parent
BA_JS_PATH = BASE_DIR / "static" / "ba.js"
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


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

app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
app.include_router(ba_router)
app.include_router(api_router)


@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    return templates.TemplateResponse("index.html", {"request": request})


@app.get("/ba.js")
async def snippet() -> FileResponse:
    return FileResponse(BA_JS_PATH, media_type="application/javascript")
