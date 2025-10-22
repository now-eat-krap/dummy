import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


BASE_DIR = Path(__file__).resolve().parent
HEATMAP_CACHE_DIR = Path(os.getenv("HEATMAP_CACHE_DIR", str(BASE_DIR / "heatmap_cache"))).expanduser()
HEATMAP_CACHE_DIR.mkdir(parents=True, exist_ok=True)


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


def _snapshot_parts(
    route_norm: str,
    snapshot_hash: str,
    vp_bucket: str,
    grid_id: str,
    section: str,
) -> Tuple[str, ...]:
    route_parts = [part for part in (route_norm or "").split("/") if part]
    if not route_parts:
        route_parts = ["root"]
    safe_route = [safe_cache_segment(part) for part in route_parts]
    return (
        safe_cache_segment(snapshot_hash or "default"),
        *safe_route,
        safe_cache_segment(vp_bucket or "any"),
        safe_cache_segment(grid_id or "grid"),
        safe_cache_segment(section or "all"),
    )


def snapshot_cache_path(
    route_norm: str,
    snapshot_hash: str,
    vp_bucket: str,
    grid_id: str,
    section: str,
    extension: str = "webp",
) -> Path:
    parts = _snapshot_parts(route_norm, snapshot_hash, vp_bucket, grid_id, section)
    filename = f"snapshot.{extension.strip('.') or 'webp'}"
    return HEATMAP_CACHE_DIR.joinpath(*parts, filename)


def snapshot_cache_relative(
    route_norm: str,
    snapshot_hash: str,
    vp_bucket: str,
    grid_id: str,
    section: str,
    extension: str = "webp",
) -> str:
    parts = _snapshot_parts(route_norm, snapshot_hash, vp_bucket, grid_id, section)
    filename = f"snapshot.{extension.strip('.') or 'webp'}"
    return str(Path(*parts, filename))


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
