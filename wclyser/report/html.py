"""Rendering an analysis result as a self-contained HTML page.

The page has to open on a machine that has none of this installed, so the
template carries its own CSS and script and the data is embedded as JSON. The
only external request is the Google Fonts stylesheet, and the page falls back
to system faces without it.

A template is per module rather than generic. The Havoc page draws a raster of
cast marks against a window, which no general table renderer would produce, so
pretending otherwise would buy nothing.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from ..errors import WCLError

TEMPLATE_DIR = Path(__file__).parent / "templates"

# Modules that can be rendered as a page, and the template each uses.
TEMPLATES = {"havoc": "havoc.html"}

_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


def available(module_key: str) -> bool:
    return module_key in TEMPLATES


def _escape_for_script(payload: str) -> str:
    """Keep embedded JSON from closing the script element early."""
    return payload.replace("</", "<\\/")


def render(module_key: str, payload: dict, footer_note: str = "") -> str:
    template_name = TEMPLATES.get(module_key)
    if template_name is None:
        raise WCLError(
            f"no page template for analysis module {module_key!r}; "
            f"templates exist for: {', '.join(sorted(TEMPLATES))}"
        )
    path = TEMPLATE_DIR / template_name
    if not path.is_file():
        raise WCLError(f"template missing from the package: {path}")

    html = path.read_text(encoding="utf-8")
    title = payload.get("title") or "Havoc windows"
    body = _escape_for_script(json.dumps(payload, separators=(",", ":")))

    html = html.replace("{{TITLE}}", title)
    html = html.replace("{{FOOTER_NOTE}}", footer_note or "wclyser")
    html = html.replace("/*__DATA__*/", body)
    return html


def filename(report_code: str, player_key: str, module_key: str) -> str:
    slug = _UNSAFE.sub("-", player_key).strip("-")
    return f"{report_code}-{slug}-{module_key}.html"


def write(path: Path, html: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(html, encoding="utf-8")
    return path
