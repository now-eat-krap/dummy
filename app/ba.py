import logging
import os
import re
import time
from typing import Any, Dict

import httpx
from fastapi import APIRouter, Request, Response

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

    try:
        payload = await request.json()
    except Exception:
        return Response(status_code=204)

    if not isinstance(payload, dict):
        return Response(status_code=204)

    event = payload
    site = str(event.get("site") or "default").strip() or "default"
    event_type = str(event.get("type") or "event").strip() or "event"
    route = _normalize_route(event.get("route") or event.get("path") or event.get("url"))

    depth = _coerce_int(event.get("depth"), 0)
    sec = _coerce_int(event.get("sec"), 0)

    vp = event.get("vp") if isinstance(event.get("vp"), dict) else {}
    vp_w = _coerce_int(vp.get("w") if isinstance(vp, dict) else None, 0)
    vp_h = _coerce_int(vp.get("h") if isinstance(vp, dict) else None, 0)
    vp_dpr = _coerce_float(vp.get("dpr") if isinstance(vp, dict) else None, 0.0)

    path_field = event.get("path") or event.get("url") or route

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

    line = (
        f"logflow,site={_escape_tag(site)},t={_escape_tag(event_type)},route={_escape_tag(route)} "
        f"{','.join(fields)} {timestamp_ms}"
    )

    try:
        await _write_line(request, line, cfg)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("Unexpected error writing analytics event: %s", exc)

    return Response(status_code=204)
