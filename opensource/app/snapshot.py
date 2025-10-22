import logging
import os
import time
from typing import Any, Dict, Optional
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field

from .ba import _normalize_route
from .cache_utils import HEATMAP_CACHE_DIR, snapshot_cache_path, snapshot_cache_relative, write_metadata

SNAPSHOT_WORKER_URL = os.getenv("SNAPSHOT_WORKER_URL", "http://localhost:9230").rstrip("/")
SNAPSHOT_WORKER_TIMEOUT = float(os.getenv("SNAPSHOT_WORKER_TIMEOUT", "45.0"))
DEFAULT_VIEWPORT_WIDTH = int(os.getenv("SNAPSHOT_VIEWPORT_WIDTH", "1440"))
DEFAULT_VIEWPORT_HEIGHT = int(os.getenv("SNAPSHOT_VIEWPORT_HEIGHT", "900"))
DEFAULT_DEVICE_SCALE = float(os.getenv("SNAPSHOT_DEVICE_SCALE", "1.0"))

logger = logging.getLogger("uvicorn.error")
router = APIRouter(prefix="/snapshot", tags=["snapshot"])


class ViewportPayload(BaseModel):
    width: Optional[int] = Field(default=None, ge=240, le=8192)
    height: Optional[int] = Field(default=None, ge=240, le=8192)
    device_scale_factor: Optional[float] = Field(default=None, ge=0.1, le=4.0, alias="dpr")

    class Config:
        allow_population_by_field_name = True


class SnapshotRequestPayload(BaseModel):
    url: str = Field(..., min_length=1)
    site: Optional[str] = Field(default=None, max_length=120)
    route: Optional[str] = Field(default=None, max_length=320)
    snapshot_hash: Optional[str] = Field(default="default", alias="snapshot", max_length=120)
    vp_bucket: Optional[str] = Field(default=None, alias="vp", max_length=60)
    grid_id: Optional[str] = Field(default=None, alias="grid", max_length=60)
    section: Optional[str] = Field(default=None, max_length=60)
    viewport: Optional[ViewportPayload] = None

    class Config:
        allow_population_by_field_name = True


@router.post("/request", status_code=status.HTTP_200_OK)
async def request_snapshot(payload: SnapshotRequestPayload, request: Request) -> Dict[str, Any]:
    target_url = payload.url.strip()
    parsed = urlparse(target_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise HTTPException(status_code=400, detail="Invalid URL")

    raw_route = payload.route or parsed.path or "/"
    route_value = _normalize_route(raw_route)
    site_value = _clean_token(payload.site, default="default", limit=120)
    snapshot_hash = _clean_token(payload.snapshot_hash, default="default", limit=80)
    vp_bucket = _clean_token(payload.vp_bucket, default="any", limit=40)
    grid_id = _clean_token(payload.grid_id, default=_default_grid_id(), limit=40)
    section_value = _clean_token(payload.section, default="all", limit=40)

    viewport = payload.viewport or ViewportPayload()
    vp_width = viewport.width or DEFAULT_VIEWPORT_WIDTH
    vp_height = viewport.height or DEFAULT_VIEWPORT_HEIGHT
    device_scale = viewport.device_scale_factor or DEFAULT_DEVICE_SCALE

    cache_path = snapshot_cache_path(route_value, snapshot_hash, vp_bucket, grid_id, section_value)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    rel_path = snapshot_cache_relative(route_value, snapshot_hash, vp_bucket, grid_id, section_value)

    job_payload = {
        "url": target_url,
        "output": rel_path,
        "fullPage": True,
        "viewport": {
            "width": vp_width,
            "height": vp_height,
            "deviceScaleFactor": device_scale,
        },
    }

    worker_endpoint = f"{SNAPSHOT_WORKER_URL}/capture"
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(SNAPSHOT_WORKER_TIMEOUT, connect=5.0)) as client:
            response = await client.post(worker_endpoint, json=job_payload)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("Snapshot worker request failed: %s", exc)
        raise HTTPException(status_code=502, detail="Snapshot worker unavailable") from exc

    if response.status_code >= 400:
        logger.warning("Snapshot worker rejected job (%s): %s", response.status_code, response.text)
        raise HTTPException(status_code=502, detail="Snapshot worker rejected request")

    try:
        result = response.json()
    except ValueError as exc:
        raise HTTPException(status_code=502, detail="Snapshot worker returned invalid payload") from exc

    if not result.get("ok"):
        raise HTTPException(status_code=502, detail=result.get("error") or "Snapshot worker failed")

    captured_at = int(result.get("captured_at") or time.time() * 1000)
    width = int(result.get("width") or vp_width)
    height = int(result.get("height") or vp_height)
    size_bytes = int(result.get("bytes") or 0)
    duration_ms = result.get("duration_ms")
    media_format = str(result.get("format") or "webp").lower()
    sha256 = result.get("sha256")

    metadata: Dict[str, Any] = {
        "route": raw_route or route_value,
        "route_norm": route_value,
        "snapshot_hash": snapshot_hash,
        "vp_bucket": vp_bucket,
        "grid_id": grid_id,
        "section": section_value,
        "site": site_value,
        "captured_at": captured_at,
        "width": width,
        "height": height,
        "bytes": size_bytes,
        "duration_ms": duration_ms,
        "format": media_format,
        "sha256": sha256,
        "url": target_url,
        "cache_path": str(cache_path),
        "rel_path": rel_path,
    }
    try:
        write_metadata(cache_path, metadata)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("Failed to persist snapshot metadata for %s: %s", cache_path, exc)

    logger.info(
        "Snapshot stored %s (%sx%s, %.2f KB) in %.1f ms",
        cache_path,
        width,
        height,
        size_bytes / 1024 if size_bytes else 0,
        float(duration_ms or 0),
    )

    return {
        "ok": True,
        "route": route_value,
        "snapshot_hash": snapshot_hash,
        "vp_bucket": vp_bucket,
        "grid_id": grid_id,
        "section": section_value,
        "width": width,
        "height": height,
        "bytes": size_bytes,
        "rel_path": rel_path,
        "format": media_format,
        "captured_at": captured_at,
        "sha256": sha256,
    }


def _clean_token(value: Optional[str], *, default: str, limit: int) -> str:
    if not value:
        return default
    text = str(value).strip()
    if not text:
        return default
    sanitized = "".join(ch for ch in text if 32 <= ord(ch) <= 126)
    sanitized = sanitized[:limit]
    return sanitized or default


def _default_grid_id() -> str:
    cols = _parse_int_env("HEATMAP_COLS", default=12, minimum=1)
    rows = _parse_int_env("HEATMAP_ROWS", default=8, minimum=1)
    return f"{cols}x{rows}"


def _parse_int_env(name: str, *, default: int, minimum: int = 1) -> int:
    value = os.getenv(name)
    if value is None:
        return max(default, minimum)
    try:
        parsed = int(value)
    except ValueError:
        return max(default, minimum)
    return parsed if parsed >= minimum else minimum
