"""
Google News Tool Runner — Generates an inline HTML carousel card.

One article is shown at a time; users click arrows or swipe to navigate.
JS wiring lives in frontend/interface/cards/tool_result.js (data-carousel convention).
Outputs formalized IPC contract: {"text": str, "html": str}
"""

import sys
import json
import base64
from html import escape
from handler import execute


# ── SVG icons (inline, no external resources) ─────────────────────────────────

_LINK_ICON = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="11" height="11" viewBox="0 0 24 24" '
    'fill="none" stroke="currentColor" stroke-width="2.5" '
    'stroke-linecap="round" stroke-linejoin="round" '
    'style="vertical-align:middle;flex-shrink:0;">'
    '<path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/>'
    '<polyline points="15 3 21 3 21 9"/>'
    '<line x1="10" y1="14" x2="21" y2="3"/>'
    '</svg>'
)

_CHEVRON_LEFT = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" '
    'fill="none" stroke="currentColor" stroke-width="2.5" '
    'stroke-linecap="round" stroke-linejoin="round">'
    '<polyline points="15 18 9 12 15 6"/>'
    '</svg>'
)

_CHEVRON_RIGHT = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" '
    'fill="none" stroke="currentColor" stroke-width="2.5" '
    'stroke-linecap="round" stroke-linejoin="round">'
    '<polyline points="9 18 15 12 9 6"/>'
    '</svg>'
)

# Radiant design palette constants
_ACCENT = "#E8850C"          # Warm amber — news accent
_ACCENT_BG = "rgba(232,133,12,0.15)"
_TEXT_PRIMARY = "#eae6f2"
_TEXT_SECONDARY = "rgba(234,230,242,0.58)"
_TEXT_TERTIARY = "rgba(234,230,242,0.38)"
_SURFACE = "rgba(255,255,255,0.04)"
_BORDER = "rgba(255,255,255,0.07)"
_DOT_ACTIVE = "#8A5CFF"      # Violet — shared carousel convention
_DOT_INACTIVE = "rgba(255,255,255,0.25)"


# ── Slide rendering ───────────────────────────────────────────────────────────

def _render_slide(article: dict, visible: bool) -> str:
    title = article.get("title") or ""
    source = article.get("source") or ""
    date = article.get("date") or ""
    description = article.get("description") or ""
    url = article.get("url") or ""
    image_url = article.get("image_url") or ""
    also_reported_by = article.get("also_reported_by") or []

    display = "flex" if visible else "none"

    # Thumbnail — omitted gracefully if absent
    img_html = ""
    if image_url:
        img_html = (
            f'<img src="{escape(image_url)}" alt="" loading="lazy" '
            f'style="width:80px;height:80px;border-radius:7px;object-fit:cover;'
            f'flex-shrink:0;background:{_SURFACE};" />'
        )

    gap = "gap:14px;" if img_html else ""

    # Source · date meta line
    meta_parts = []
    if source:
        meta_parts.append(
            f'<span style="color:{_ACCENT};font-weight:600;">{escape(source)}</span>'
        )
    if date:
        meta_parts.append(
            f'<span style="color:{_TEXT_TERTIARY};">{escape(date)}</span>'
        )
    meta_html = ""
    if meta_parts:
        separator = f' <span style="color:rgba(234,230,242,0.2);">\u00b7</span> '
        meta_html = (
            f'<div style="display:flex;align-items:center;gap:6px;'
            f'font-size:11px;margin-bottom:5px;flex-wrap:wrap;">'
            + separator.join(meta_parts)
            + '</div>'
        )

    # "Major coverage" badge — shown when 3+ sources reported the same story
    badge_html = ""
    if len(also_reported_by) >= 3:
        badge_html = (
            f'<span style="display:inline-block;font-size:9px;font-weight:600;'
            f'background:{_ACCENT_BG};color:{_ACCENT};border-radius:3px;'
            f'padding:1px 6px;letter-spacing:0.04em;margin-bottom:6px;">'
            f'Major coverage</span>'
        )

    # Cross-reference line
    xref_html = ""
    if also_reported_by:
        sources_str = ", ".join(escape(s) for s in also_reported_by[:4])
        xref_html = (
            f'<div style="font-size:10px;color:{_TEXT_TERTIARY};'
            f'margin-top:5px;font-style:italic;">'
            f'Also reported by: {sources_str}</div>'
        )

    # Description snippet (truncated, HTML-escaped)
    desc_html = ""
    if description:
        desc_text = description[:220] + ("\u2026" if len(description) > 220 else "")
        desc_html = (
            f'<p style="font-size:13px;color:{_TEXT_SECONDARY};'
            f'line-height:1.55;margin:0 0 8px 0;">{escape(desc_text)}</p>'
        )

    return (
        f'<div data-slide '
        f'style="display:{display};align-items:flex-start;{gap}'
        f'padding:13px 15px;background:{_SURFACE};'
        f'border-radius:9px;border:1px solid {_BORDER};">'
        + img_html
        + f'<div style="flex:1;min-width:0;">'
        + meta_html
        + badge_html
        + f'<div style="font-weight:600;font-size:14px;color:{_TEXT_PRIMARY};'
          f'line-height:1.3;margin-bottom:5px;">{escape(title)}</div>'
        + desc_html
        + xref_html
        + f'<a href="{escape(url)}" target="_blank" rel="noopener noreferrer" '
          f'style="display:inline-flex;align-items:center;gap:5px;'
          f'color:{_ACCENT};font-size:12px;text-decoration:none;opacity:0.85;'
          f'margin-top:6px;">'
        + _LINK_ICON
        + f'<span>Read full article</span>'
        + '</a>'
        + '</div>'
        + '</div>'
    )


# ── Navigation ────────────────────────────────────────────────────────────────

def _render_navigation(count: int) -> str:
    """Carousel nav buttons + dot indicators. Matches Wikipedia's convention exactly."""
    btn_style = (
        f"background:{_SURFACE};border:1px solid rgba(255,255,255,0.12);"
        "border-radius:50%;width:28px;height:28px;display:inline-flex;align-items:center;"
        "justify-content:center;cursor:pointer;color:rgba(234,230,242,0.7);padding:0;"
        "flex-shrink:0;outline:none;"
        "transition:background 220ms ease,border-color 220ms ease,color 220ms ease;"
    )

    dots = "".join(
        f'<span data-dot style="'
        + (
            f"width:7px;height:7px;border-radius:50%;background:{_DOT_ACTIVE};"
            "transform:scale(1.2);flex-shrink:0;cursor:pointer;transition:all 220ms ease;"
            if i == 0 else
            f"width:7px;height:7px;border-radius:50%;background:{_DOT_INACTIVE};"
            "flex-shrink:0;cursor:pointer;transition:all 220ms ease;"
        )
        + '"></span>'
        for i in range(count)
    )

    return (
        '<div style="display:flex;align-items:center;justify-content:center;'
        'gap:8px;margin-top:10px;">'
        + f'<button type="button" data-prev style="{btn_style}">{_CHEVRON_LEFT}</button>'
        + f'<div style="display:flex;align-items:center;gap:5px;">{dots}</div>'
        + f'<button type="button" data-next style="{btn_style}">{_CHEVRON_RIGHT}</button>'
        + '</div>'
    )


# ── Card assembly ─────────────────────────────────────────────────────────────

def _render_html(results: list) -> str:
    """Assemble the full carousel card. Hard-capped at 8 slides."""
    results = results[:8]  # Enforce cognitive load cap
    if not results:
        return (
            f'<p style="color:{_TEXT_TERTIARY};font-size:13px;'
            f'font-family:system-ui,-apple-system,sans-serif;padding:12px 14px;margin:0;">'
            f'No news articles found.</p>'
        )

    slides = "".join(_render_slide(r, i == 0) for i, r in enumerate(results))
    nav = _render_navigation(len(results)) if len(results) > 1 else ""

    return (
        '<div data-carousel '
        'style="font-family:system-ui,-apple-system,sans-serif;">'
        + slides
        + nav
        + '</div>'
    )


# ── Text for LLM synthesis ────────────────────────────────────────────────────

def _format_text(results: list, query: str) -> str:
    """
    Structured text output — this is what the LLM receives for synthesis.
    Includes source citations and cross-references so the LLM can produce
    a balanced, multi-source narrative.
    """
    if not results:
        return (
            f'No news articles found for "{query}". '
            f'Try a different query, broaden the time range, or use a web search tool for broader coverage.'
        )

    lines = [f'News results for "{query}":']
    for i, r in enumerate(results, 1):
        lines.append(f"\n{i}. {r.get('title', '')}")
        source = r.get("source", "")
        published_at = r.get("published_at") or r.get("date", "")
        if source:
            lines.append(f"   Source: {source}" + (f" ({published_at})" if published_at else ""))
        if desc := r.get("description", ""):
            lines.append(f"   {desc}")
        if url := r.get("url", ""):
            lines.append(f"   {url}")
        if also := r.get("also_reported_by", []):
            lines.append(f"   Also reported by: {', '.join(also[:4])}")
    return "\n".join(lines)


# ── Entry point ───────────────────────────────────────────────────────────────

payload = json.loads(base64.b64decode(sys.argv[1]))
params = payload.get("params", {})
settings = payload.get("settings", {})
telemetry = payload.get("telemetry", {})

result = execute(topic="", params=params, config=settings, telemetry=telemetry)
results = result.get("results", [])

output = {
    "results": results,
    "count": result.get("count", 0),
    "query": result.get("query", ""),
    "text": _format_text(results, result.get("query", "")),
    # Only output html when there are actual results — an empty card adds noise
    # and blocks Chalie from responding with useful commentary or alternatives.
    "html": _render_html(results) if results else None,
    "_meta": result.get("_meta", {}),
}
if "error" in result:
    output["error"] = result["error"]

print(json.dumps(output))
