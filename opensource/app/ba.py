import json
import logging
import os
import re
import time
from typing import Any, Dict

import httpx
from fastapi import APIRouter, Request, Response

from .cache_utils import render_skeleton_html, snapshot_cache_path, write_metadata

router = APIRouter()
logger = logging.getLogger("uvicorn.error")

SEGMENT_RE = re.compile(r"/([0-9]+|[0-9a-fA-F]{12,})")


def _get_influx_config() -> Dict[str, str]:
    return {
        "url": os.getenv("INFLUX_URL", "http://influxdb:8086").rstrip("/"),
        "token": os.getenv("INFLUX_TOKEN", "logflow-dev-token"),
        "org": os.getenv("INFLUX_ORG", "logflow"),
        "bucket": os.getenv("INFLUX_BUCKET", "logflow"),
    }


def _normalize_route(raw: Any) -> str:
    if not raw:
        return "/"
    path = str(raw).split("?", 1)[0]
    if not path.startswith("/"):
        path = "/" + path
    path = SEGMENT_RE.sub("/:id", path)
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")
    return path or "/"


def _escape_tag(value: Any) -> str:
    text = str(value)
    text = text.replace("\\", "\\\\")
    text = text.replace(",", "\\,").replace(" ", "\\ ").replace("=", "\\=")
    return text


def _escape_field(value: Any) -> str:
    text = str(value)
    text = text.replace("\\", "\\\\")
    return text.replace('"', '\\"')


def _coerce_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _coerce_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _format_float(value: float, precision: int = 6) -> str:
    text = f"{value:.{precision}f}"
    text = text.rstrip("0").rstrip(".")
    return text or "0"


async def _write_line(request: Request, line: str, cfg: Dict[str, str]) -> None:
    params = {"org": cfg["org"], "bucket": cfg["bucket"], "precision": "ms"}
    headers = {
        "Authorization": f"Token {cfg['token']}",
        "Content-Type": "text/plain; charset=utf-8",
    }
    client: httpx.AsyncClient | None = getattr(request.app.state, "http_client", None)
    if client is not None:
        try:
            await client.post(
                f"{cfg['url']}/api/v2/write",
                params=params,
                content=line,
                headers=headers,
            )
            return
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("Influx write via shared client failed: %s", exc)
    async with httpx.AsyncClient(timeout=httpx.Timeout(5.0, connect=2.0)) as temp_client:
        try:
            await temp_client.post(
                f"{cfg['url']}/api/v2/write",
                params=params,
                content=line,
                headers=headers,
            )
        except Exception as exc:
            logger.warning("Failed to write analytics event to Influx: %s", exc)


@router.post("/ba", status_code=204)
async def ingest(request: Request) -> Response:
    cfg = _get_influx_config()

    client_host = ""
    if request.client:
        client_host = request.client.host or ""

    try:
        payload = await request.json()
    except Exception:
        return Response(status_code=204)

    if not isinstance(payload, dict):
        return Response(status_code=204)

    event = payload
    site = str(event.get("site") or "default").strip() or "default"
    raw_event_type = str(event.get("type") or "event").strip()
    event_type = raw_event_type or "event"
    if event_type.lower() == "heartbeat":
        return Response(status_code=204)
    route = _normalize_route(event.get("route") or event.get("path") or event.get("url"))
    route_norm_raw = str(event.get("route_norm") or "").strip()
    if route_norm_raw:
        route_norm = _normalize_route(route_norm_raw)
    else:
        route_norm = route

    depth = _coerce_int(event.get("depth"), 0)
    sec = _coerce_int(event.get("sec"), 0)

    vp = event.get("vp") if isinstance(event.get("vp"), dict) else {}
    vp_w = _coerce_int(vp.get("w") if isinstance(vp, dict) else None, 0)
    vp_h = _coerce_int(vp.get("h") if isinstance(vp, dict) else None, 0)
    vp_dpr = _coerce_float(vp.get("dpr") if isinstance(vp, dict) else None, 0.0)

    path_field = event.get("path") or event.get("url") or route
    element = str(event.get("element") or "").strip()

    coords = event.get("coords") if isinstance(event.get("coords"), dict) else {}
    raw_cx = coords.get("x") if isinstance(coords, dict) else None
    raw_cy = coords.get("y") if isinstance(coords, dict) else None
    raw_px = coords.get("pageX") if isinstance(coords, dict) else None
    raw_py = coords.get("pageY") if isinstance(coords, dict) else None
    cx = _coerce_int(raw_cx, 0)
    cy = _coerce_int(raw_cy, 0)
    page_x = _coerce_int(raw_px, 0)
    page_y = _coerce_int(raw_py, 0)
    has_coords = any(value is not None for value in (raw_cx, raw_cy, raw_px, raw_py))
    section = str(event.get("section") or "").strip()
    element_text = str(event.get("element_text") or "").strip()
    el_hash = str(event.get("el_hash") or "").strip()
    x_bin_value = event.get("x_bin")
    y_bin_value = event.get("y_bin")
    doc_x_value = _coerce_float(event.get("doc_x"), -1.0)
    doc_y_value = _coerce_float(event.get("doc_y"), -1.0)
    doc_w = _coerce_int(event.get("doc_w"), 0)
    doc_h = _coerce_int(event.get("doc_h"), 0)
    scroll_top = _coerce_int(event.get("scroll_top"), 0) if "scroll_top" in event else None
    scroll_height = _coerce_int(event.get("scroll_height"), 0) if "scroll_height" in event else None
    viewport_height = _coerce_int(event.get("viewport_height"), 0) if "viewport_height" in event else None
    snapshot_hash = str(event.get("snapshot_hash") or "").strip() or "default"
    vp_bucket = str(event.get("vp_bucket") or "").strip()
    grid_id = str(event.get("grid_id") or "").strip()
    if not grid_id:
        grid_id = "default"

    timestamp_ms = int(time.time() * 1000)
    for key in ("ts", "timestamp"):
        if key in event:
            try:
                timestamp_ms = int(float(event[key]))
                break
            except (TypeError, ValueError):
                continue

    fields = [
        "count=1i",
        f"depth={depth}i",
        f"sec={sec}i",
        f"vp_w={vp_w}i",
        f"vp_h={vp_h}i",
    ]
    dpr_str = f"{vp_dpr:.3f}".rstrip("0").rstrip(".") or "0"
    fields.append(f"vp_dpr={dpr_str}")
    fields.append(f'path="{_escape_field(path_field)}"')
    if element:
        fields.append(f'element="{_escape_field(element)}"')
    if has_coords:
        fields.append(f"cx={cx}i")
        fields.append(f"cy={cy}i")
        if raw_px is not None:
            fields.append(f"px={page_x}i")
        if raw_py is not None:
            fields.append(f"py={page_y}i")
    if doc_x_value >= 0:
        fields.append(f"doc_x={_format_float(doc_x_value)}")
    if doc_y_value >= 0:
        fields.append(f"doc_y={_format_float(doc_y_value)}")
    if doc_w > 0:
        fields.append(f"doc_w={doc_w}i")
    if doc_h > 0:
        fields.append(f"doc_h={doc_h}i")
    if snapshot_hash:
        fields.append(f'snapshot="{_escape_field(snapshot_hash)}"')
    if vp_bucket:
        fields.append(f'vp_bucket="{_escape_field(vp_bucket)}"')
    if grid_id:
        fields.append(f'grid="{_escape_field(grid_id)}"')
    if el_hash:
        fields.append(f'el_hash="{_escape_field(el_hash)}"')

    payload_data: Dict[str, Any] = {
        "site": site,
        "type": event_type,
        "route": route,
        "route_norm": route_norm,
        "path": path_field,
        "source": event.get("source"),
        "trigger": event.get("trigger"),
        "depth": depth,
        "sec": sec,
        "ts": timestamp_ms,
        "snapshot_hash": snapshot_hash,
        "grid_id": grid_id,
        "vp_bucket": vp_bucket,
    }
    if client_host:
        payload_data["ip"] = client_host
    if element:
        payload_data["element"] = element
    if element_text:
        payload_data["element_text"] = element_text
    if el_hash:
        payload_data["el_hash"] = el_hash
    coords_payload: Dict[str, Any] = {}
    if raw_cx is not None:
        coords_payload["x"] = cx
    if raw_cy is not None:
        coords_payload["y"] = cy
    if raw_px is not None:
        coords_payload["pageX"] = page_x
    if raw_py is not None:
        coords_payload["pageY"] = page_y
    if coords_payload:
        payload_data["coords"] = coords_payload
    if section:
        payload_data["section"] = section
    if x_bin_value is not None:
        payload_data["x_bin"] = _coerce_int(x_bin_value, 0)
    if y_bin_value is not None:
        payload_data["y_bin"] = _coerce_int(y_bin_value, 0)
    if doc_x_value >= 0:
        payload_data["doc_x"] = doc_x_value
    if doc_y_value >= 0:
        payload_data["doc_y"] = doc_y_value
    if doc_w > 0:
        payload_data["doc_w"] = doc_w
    if doc_h > 0:
        payload_data["doc_h"] = doc_h
    if "scroll_top" in event:
        payload_data["scroll_top"] = scroll_top or 0
    if "scroll_height" in event:
        payload_data["scroll_height"] = scroll_height or 0
    if "viewport_height" in event:
        payload_data["viewport_height"] = viewport_height or 0
    event_id = event.get("event_id")
    if isinstance(event_id, str) and event_id:
        payload_data["event_id"] = event_id
    uid = event.get("uid")
    if isinstance(uid, str) and uid:
        payload_data["uid"] = uid
    sid = event.get("sid")
    if isinstance(sid, str) and sid:
        payload_data["sid"] = sid
    if vp_w or vp_h or vp_dpr:
        payload_data["vp"] = {"w": vp_w, "h": vp_h, "dpr": vp_dpr}

    try:
        payload_json = json.dumps(payload_data, separators=(",", ":"))
        fields.append(f'payload="{_escape_field(payload_json)}"')
    except (TypeError, ValueError):
        pass
    line = (
        f"logflow,site={_escape_tag(site)},t={_escape_tag(event_type)},route={_escape_tag(route)} "
        f"{','.join(fields)} {timestamp_ms}"
    )

    try:
        await _write_line(request, line, cfg)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("Unexpected error writing analytics event: %s", exc)

    if event_type == "click":
        x_bin = _coerce_int(event.get("x_bin"), -1)
        y_bin = _coerce_int(event.get("y_bin"), -1)
        section_value = section or "unspecified"
        if x_bin >= 0 and y_bin >= 0:
            click_fields = [
                "count=1i",
                f"x_bin={x_bin}i",
                f"y_bin={y_bin}i",
            ]
            if doc_x_value >= 0:
                click_fields.append(f"doc_x={_format_float(doc_x_value)}")
            if doc_y_value >= 0:
                click_fields.append(f"doc_y={_format_float(doc_y_value)}")
            if doc_w > 0:
                click_fields.append(f"doc_w={doc_w}i")
            if doc_h > 0:
                click_fields.append(f"doc_h={doc_h}i")
            if raw_px is not None:
                click_fields.append(f"px={page_x}i")
            if raw_py is not None:
                click_fields.append(f"py={page_y}i")
            click_tags = [
                ("site", site),
                ("route", route),
                ("route_norm", route_norm),
                ("section", section_value),
                ("snapshot", snapshot_hash),
                ("grid", grid_id),
            ]
            if vp_bucket:
                click_tags.append(("vp", vp_bucket))
            if el_hash:
                click_tags.append(("el", el_hash))
            tag_str = ",".join(f"{name}={_escape_tag(value)}" for name, value in click_tags if value)
            click_line = f"logflow_click,{tag_str} {','.join(click_fields)} {timestamp_ms}"
            try:
                await _write_line(request, click_line, cfg)
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning("Unexpected error writing click analytics event: %s", exc)

    return Response(status_code=204)


@router.post("/ba/snapshot", status_code=204)
async def ingest_snapshot(request: Request) -> Response:
    try:
        payload = await request.json()
    except Exception:
        return Response(status_code=204)

    if not isinstance(payload, dict):
        return Response(status_code=204)

    route = _normalize_route(payload.get("route") or payload.get("path") or payload.get("url"))
    route_norm_raw = str(payload.get("route_norm") or "").strip()
    if route_norm_raw:
        route_norm = _normalize_route(route_norm_raw)
    else:
        route_norm = route

    site_value = str(payload.get("site") or "").strip()
    snapshot_hash = str(payload.get("snapshot_hash") or "default").strip() or "default"
    vp_bucket = str(payload.get("vp_bucket") or payload.get("vp") or "").strip() or "any"
    grid_id = str(payload.get("grid_id") or payload.get("grid") or "").strip() or "grid"
    section = str(payload.get("section") or "").strip() or "all"

    skeleton_payload = payload.get("skeleton")
    if not isinstance(skeleton_payload, dict):
        return Response(status_code=204)

    skeleton_copy = dict(skeleton_payload)
    if "captured_at" not in skeleton_copy:
        skeleton_copy["captured_at"] = int(time.time() * 1000)
    if "label" not in skeleton_copy:
        if site_value:
            skeleton_copy["label"] = f"{site_value} · {route_norm}"
        else:
            skeleton_copy["label"] = f"{route_norm} · {snapshot_hash}"

    try:
        html = render_skeleton_html(skeleton_copy)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("Skeleton serialization failed: %s", exc)
        return Response(status_code=204)

    cache_path = snapshot_cache_path(route_norm, snapshot_hash, vp_bucket, grid_id, section)
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(html, encoding="utf-8")
        logger.info("Stored snapshot cache at %s", cache_path)
        metadata = {
            "route": payload.get("route") or route,
            "route_norm": route_norm,
            "snapshot_hash": snapshot_hash,
            "vp_bucket": vp_bucket,
            "grid_id": grid_id,
            "section": section,
            "site": site_value or "default",
            "boxes": len(skeleton_copy.get("boxes") or []),
            "captured_at": skeleton_copy.get("captured_at"),
            "label": skeleton_copy.get("label"),
            "viewport": skeleton_copy.get("viewport"),
            "cache_path": str(cache_path),
        }
        write_metadata(cache_path, metadata)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("Failed to persist snapshot cache: %s", exc)
        return Response(status_code=204)

    return Response(status_code=204)
