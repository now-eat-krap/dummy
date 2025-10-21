import json
import os
import time
from html import escape
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


BASE_DIR = Path(__file__).resolve().parent
HEATMAP_CACHE_DIR = Path(os.getenv("HEATMAP_CACHE_DIR", str(BASE_DIR / "heatmap_cache"))).expanduser()
HEATMAP_CACHE_DIR.mkdir(parents=True, exist_ok=True)

_SKELETON_STYLE = """
:root {
  color-scheme: dark;
  font-family: "Inter", system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
}
* {
  box-sizing: border-box;
}
body {
  margin: 0;
  padding: 28px;
  background: radial-gradient(circle at top, #1f2937 2%, #0b1120 68%);
  color: #94a3b8;
  display: flex;
  flex-direction: column;
  gap: 18px;
}
.sk-info {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  padding: 6px 14px;
  border-radius: 999px;
  background: rgba(148, 163, 184, 0.18);
  border: 1px solid rgba(148, 163, 184, 0.28);
  font-size: 0.72rem;
  letter-spacing: 0.08em;
  text-transform: uppercase;
}
.sk-stage {
  position: relative;
  width: min(100%, 960px);
  aspect-ratio: var(--sk-width) / var(--sk-height);
  border-radius: 20px;
  overflow: hidden;
  background: rgba(15, 23, 42, 0.68);
  box-shadow: 0 40px 80px -40px rgba(15, 23, 42, 0.8);
  border: 1px solid rgba(148, 163, 184, 0.14);
}
.sk-grid {
  position: absolute;
  inset: 0;
  pointer-events: none;
}
.sk-box {
  position: absolute;
  left: calc(var(--x) * 100%);
  top: calc(var(--y) * 100%);
  width: max(calc(var(--w) * 100%), 2px);
  height: max(calc(var(--h) * 100%), 2px);
  border-radius: 12px;
  background: rgba(148, 163, 184, 0.12);
  border: 1px solid rgba(148, 163, 184, 0.22);
  backdrop-filter: saturate(120%);
  display: flex;
  align-items: flex-start;
  justify-content: flex-start;
  padding: 14px;
  overflow: hidden;
}
.sk-box:hover {
  border-color: rgba(250, 204, 21, 0.6);
}
.sk-box::after {
  content: attr(data-kind);
  position: absolute;
  bottom: 10px;
  right: 12px;
  font-size: 0.62rem;
  letter-spacing: 0.1em;
  text-transform: uppercase;
  color: rgba(148, 163, 184, 0.5);
}
.sk-box .sk-label {
  font-size: 0.65rem;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  padding: 4px 8px;
  border-radius: 999px;
  background: rgba(148, 163, 184, 0.18);
  border: 1px solid rgba(148, 163, 184, 0.28);
  color: rgba(226, 232, 240, 0.9);
}
.sk-box.media {
  background: rgba(59, 130, 246, 0.16);
  border-color: rgba(59, 130, 246, 0.45);
}
.sk-box.button,
.sk-box.input {
  background: rgba(16, 185, 129, 0.14);
  border-color: rgba(16, 185, 129, 0.4);
}
.sk-box.heading,
.sk-box.text {
  background: rgba(236, 72, 153, 0.12);
  border-color: rgba(236, 72, 153, 0.38);
}
.sk-box.nav,
.sk-box.header {
  background: rgba(8, 145, 178, 0.18);
  border-color: rgba(8, 145, 178, 0.45);
}
.sk-box.footer,
.sk-box.aside {
  background: rgba(129, 140, 248, 0.14);
  border-color: rgba(129, 140, 248, 0.42);
}
.sk-box.card,
.sk-box.section,
.sk-box.panel {
  background: rgba(148, 163, 184, 0.1);
  border-color: rgba(148, 163, 184, 0.28);
}
.sk-box.list,
.sk-box.table {
  background: rgba(247, 171, 10, 0.16);
  border-color: rgba(247, 171, 10, 0.45);
}
"""

_VALID_KINDS = {
    "aside",
    "button",
    "card",
    "footer",
    "form",
    "header",
    "heading",
    "input",
    "list",
    "main",
    "media",
    "nav",
    "panel",
    "section",
    "table",
    "text",
}


def safe_cache_segment(value: str) -> str:
    text = (value or "default").strip()
    if not text:
        text = "default"
    text = text.replace("\\", "/")
    tokens: List[str] = []
    for ch in text:
        if ch.isalnum() or ch in {"-", "_", "."}:
            tokens.append(ch)
        elif ch == "/":
            tokens.append("__")
        else:
            tokens.append("_")
    cleaned = "".join(tokens).strip("_")
    if not cleaned:
        cleaned = "default"
    return cleaned[:80]


def snapshot_cache_path(route_norm: str, snapshot_hash: str, vp_bucket: str, grid_id: str, section: str) -> Path:
    route_parts = [part for part in (route_norm or "").split("/") if part]
    if not route_parts:
        route_parts = ["root"]
    safe_route = [safe_cache_segment(part) for part in route_parts]
    parts = [
        safe_cache_segment(snapshot_hash or "default"),
        *safe_route,
        safe_cache_segment(vp_bucket or "any"),
        safe_cache_segment(grid_id or "grid"),
        safe_cache_segment(section or "all"),
    ]
    return HEATMAP_CACHE_DIR.joinpath(*parts, "index.html")


def render_skeleton_html(skeleton: Dict[str, Any]) -> str:
    boxes = _sanitize_boxes(skeleton.get("boxes") or [])
    if not boxes:
        raise ValueError("Skeleton payload missing boxes")
    viewport = skeleton.get("viewport") or {}
    width = _coerce_positive(viewport.get("width") or viewport.get("w"), 1280)
    height = _coerce_positive(viewport.get("height") or viewport.get("h"), 720)
    if width <= 0:
        width = 1280
    if height <= 0:
        height = 720
    label = skeleton.get("label")
    info_banner = ""
    if label:
        info_banner = (
            '<div class="sk-info">'
            f"{escape(str(label)[:64])}"
            "</div>"
        )
    elif skeleton.get("captured_at"):
        captured = time.strftime(
            "%Y-%m-%d %H:%M",
            time.localtime(float(skeleton["captured_at"]) / 1000.0),
        )
        info_banner = (
            '<div class="sk-info">'
            f"Captured · {escape(captured)}"
            "</div>"
        )

    box_html = "".join(_render_box(box) for box in boxes)
    html = (
        "<!DOCTYPE html><html><head><meta charset=\"utf-8\">"
        f"<style>{_SKELETON_STYLE}</style>"
        "</head><body>"
        f"{info_banner}"
        f'<div class="sk-stage" style="--sk-width:{width};--sk-height:{height};">'
        '<div class="sk-grid">'
        f"{box_html}"
        "</div></div>"
        "</body></html>"
    )
    return html


def write_metadata(cache_path: Path, metadata: Dict[str, Any]) -> None:
    meta_path = cache_path.with_name("meta.json")
    meta_path.parent.mkdir(parents=True, exist_ok=True)
    meta_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")


def load_metadata(snapshot_hash: Optional[str] = None) -> List[Dict[str, Any]]:
    entries: List[Dict[str, Any]] = []
    base = HEATMAP_CACHE_DIR
    if not base.exists():
        return entries
    for meta_path in base.rglob("meta.json"):
        try:
            data = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if snapshot_hash and data.get("snapshot_hash") != snapshot_hash:
            continue
        entries.append(data)
    return entries


def _sanitize_boxes(raw_boxes: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    boxes: List[Dict[str, Any]] = []
    for raw in raw_boxes:
        if not isinstance(raw, dict):
            continue
        try:
            x = _clamp_float(raw.get("x"), 0.0, 1.0)
            y = _clamp_float(raw.get("y"), 0.0, 1.0)
            w = _clamp_float(raw.get("w"), 0.0, 1.0)
            h = _clamp_float(raw.get("h"), 0.0, 1.0)
        except (TypeError, ValueError):
            continue
        if w <= 0 or h <= 0:
            continue
        kind = str(raw.get("kind") or "panel").lower()
        if kind not in _VALID_KINDS:
            kind = "panel"
        boxes.append(
            {
                "tag": str(raw.get("tag") or kind).lower(),
                "kind": kind,
                "x": x,
                "y": y,
                "w": w,
                "h": h,
                "label": str(raw.get("label") or "").strip()[:40],
            }
        )
        if len(boxes) >= 240:
            break
    boxes.sort(key=lambda item: item["w"] * item["h"], reverse=True)
    return boxes


def _render_box(box: Dict[str, Any]) -> str:
    label_html = ""
    if box.get("label"):
        label_html = f'<span class="sk-label">{escape(box["label"])}</span>'
    return (
        f'<div class="sk-box {escape(box["kind"])}" data-kind="{escape(box["kind"])}"'
        f' style="--x:{box["x"]:.4f};--y:{box["y"]:.4f};--w:{box["w"]:.4f};--h:{box["h"]:.4f};">'
        f"{label_html}"
        "</div>"
    )


def _coerce_positive(value: Any, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return float(default)
    if number <= 0:
        return float(default)
    return number


def _clamp_float(value: Any, minimum: float, maximum: float) -> float:
    number = float(value)
    if number < minimum:
        return minimum
    if number > maximum:
        return maximum
    return number
