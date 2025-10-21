import os
from html import escape
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


BASE_DIR = Path(__file__).resolve().parent
HEATMAP_CACHE_DIR = Path(os.getenv("HEATMAP_CACHE_DIR", str(BASE_DIR / "heatmap_cache"))).expanduser()
HEATMAP_CACHE_DIR.mkdir(parents=True, exist_ok=True)


_BLOCK_TAGS = {
    "article",
    "aside",
    "blockquote",
    "body",
    "button",
    "canvas",
    "div",
    "fieldset",
    "figcaption",
    "figure",
    "footer",
    "form",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "header",
    "li",
    "main",
    "nav",
    "ol",
    "p",
    "pre",
    "section",
    "table",
    "tbody",
    "td",
    "tfoot",
    "th",
    "thead",
    "tr",
    "ul",
}
_INLINE_TAGS = {
    "a",
    "b",
    "code",
    "em",
    "i",
    "label",
    "small",
    "span",
    "strong",
    "sub",
    "sup",
}
_PLACEHOLDER_TAGS = {"img", "iframe", "svg", "picture", "video", "audio", "canvas"}
_TEXT_PARENT_INLINE = {"a", "button", "label", "span", "small", "strong", "em"}
_LABEL_ATTRS = ("data-section", "aria-label", "title", "alt")
_MAX_SERIALIZED_NODES = 4000

_WIREFRAME_STYLE = """
:root {
  color-scheme: dark;
  font-family: "Inter", system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
}
* {
  box-sizing: border-box;
}
body {
  margin: 0;
  padding: 32px;
  background: radial-gradient(circle at top, #1f2937 2%, #0b1120 68%);
  color: #94a3b8;
  display: flex;
  flex-direction: column;
  gap: 16px;
}
.wf-block {
  position: relative;
  background: rgba(148, 163, 184, 0.12);
  border: 1px solid rgba(148, 163, 184, 0.28);
  border-radius: 14px;
  padding: 18px;
  min-height: 28px;
  overflow: hidden;
}
.wf-inline {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  padding: 6px 10px;
  min-width: 24px;
  min-height: 18px;
  margin: 4px 6px 0 0;
  background: rgba(148, 163, 184, 0.18);
  border: 1px solid rgba(148, 163, 184, 0.35);
  border-radius: 10px;
}
.wf-heading {
  border-color: rgba(239, 68, 68, 0.45);
  background: rgba(239, 68, 68, 0.12);
}
.wf-placeholder {
  display: flex;
  align-items: center;
  justify-content: center;
  background: rgba(59, 130, 246, 0.15);
  border: 1px dashed rgba(37, 99, 235, 0.45);
  border-radius: 16px;
  padding: 18px;
  min-height: 64px;
  text-transform: uppercase;
  letter-spacing: 0.08em;
  font-size: 0.68rem;
  color: rgba(148, 163, 184, 0.9);
}
.wf-text {
  display: inline-block;
  height: 10px;
  min-width: 36px;
  border-radius: 999px;
  background: rgba(148, 163, 184, 0.35);
  margin: 4px 8px 4px 0;
}
.wf-label {
  position: absolute;
  top: 8px;
  right: 12px;
  font-size: 0.65rem;
  letter-spacing: 0.12em;
  text-transform: uppercase;
  color: rgba(148, 163, 184, 0.68);
  pointer-events: none;
}
.wf-tag {
  position: absolute;
  bottom: 8px;
  right: 12px;
  font-size: 0.62rem;
  color: rgba(148, 163, 184, 0.4);
  text-transform: uppercase;
  letter-spacing: 0.1em;
}
.wf-flex {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
}
"""


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


def build_wireframe_html(snapshot: Dict[str, Any]) -> str:
    node = snapshot.get("node") if isinstance(snapshot, dict) else None
    if not isinstance(node, dict):
        raise ValueError("Snapshot payload missing node data")
    body_node = _find_first(node, lambda item: _is_element(item, "body"))
    if body_node is None:
        raise ValueError("Snapshot body element not found")
    rendered_body, _ = _render_children(body_node.get("childNodes", []), {"count": 0})
    label = snapshot.get("label") or ""
    info_banner = ""
    if label:
        info_banner = (
            '<div class="wf-inline" style="align-self:flex-start;letter-spacing:0.08em;font-size:0.7rem;">'
            f"{escape(str(label)[:48])}"
            "</div>"
        )
    html = (
        "<!DOCTYPE html><html><head><meta charset=\"utf-8\">"
        f"<style>{_WIREFRAME_STYLE}</style>"
        "</head><body>"
        f"{info_banner}{rendered_body}"
        "</body></html>"
    )
    return html


def _render_children(nodes: Iterable[Dict[str, Any]], state: Dict[str, int]) -> Tuple[str, bool]:
    html_parts: List[str] = []
    truncated = False
    for node in nodes or []:
        if state["count"] >= _MAX_SERIALIZED_NODES:
            truncated = True
            break
        snippet, _ = _render_node(node, state)
        if snippet:
            html_parts.append(snippet)
    if truncated:
        html_parts.append(
            '<div class="wf-placeholder">Snapshot truncated for brevity</div>'
        )
    return "".join(html_parts), truncated


def _render_node(node: Dict[str, Any], state: Dict[str, int]) -> Tuple[str, bool]:
    node_type = node.get("type")
    if node_type == 0:  # Document
        return _render_children(node.get("childNodes", []), state)
    if node_type == 1:  # DocumentType
        return "", False
    if node_type == 2:  # Element
        return _render_element(node, state), False
    if node_type == 3:  # Text
        text = (node.get("textContent") or "").strip()
        if not text:
            return "", False
        state["count"] += 1
        length = min(len(text), 60)
        return f'<span class="wf-text" data-len="{length}"></span>', False
    return "", False


def _render_element(node: Dict[str, Any], state: Dict[str, int]) -> str:
    tag = (node.get("tagName") or "div").lower()
    state["count"] += 1
    if tag in {"script", "style", "noscript", "meta", "link"}:
        return ""
    if tag in _PLACEHOLDER_TAGS:
        label = tag.upper()
        attrs = node.get("attributes") or {}
        for attr_name in _LABEL_ATTRS:
            if attr_name in attrs:
                label = f"{label} · {attrs[attr_name][:28]}"
                break
        return f'<div class="wf-placeholder">{escape(label)}</div>'

    classes = ["wf-block"]
    if tag in _INLINE_TAGS:
        classes = ["wf-inline"]
    elif tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
        classes.append("wf-heading")
    labels: List[str] = []
    attributes = node.get("attributes") or {}
    for attr_name in _LABEL_ATTRS:
        if attr_name in attributes:
            labels.append(attributes[attr_name])
            break
    attrs: List[str] = [f'class="{" ".join(classes)}"']
    attrs.append(f'data-tag="{escape(tag)}"')
    if tag in {"td", "th"}:
        if "colspan" in attributes:
            attrs.append(f'colspan="{escape(attributes["colspan"])}"')
        if "rowspan" in attributes:
            attrs.append(f'rowspan="{escape(attributes["rowspan"])}"')

    children_html, truncated = _render_children(node.get("childNodes", []), state)
    label_html = ""
    if labels:
        label_html = f'<span class="wf-label">{escape(labels[0][:48])}</span>'
    tag_html = f'<span class="wf-tag">{escape(tag)}</span>' if tag not in _INLINE_TAGS else ""
    html = f"<{tag} {' '.join(attrs)}>{label_html}{children_html}{tag_html}</{tag}>"
    if truncated:
        html += '<div class="wf-placeholder">Content truncated</div>'
    return html


def _find_first(node: Dict[str, Any], predicate) -> Optional[Dict[str, Any]]:
    if predicate(node):
        return node
    for child in node.get("childNodes", []) or []:
        found = _find_first(child, predicate)
        if found is not None:
            return found
    return None


def _is_element(node: Dict[str, Any], tag_name: str) -> bool:
    return node.get("type") == 2 and (node.get("tagName") or "").lower() == tag_name.lower()
