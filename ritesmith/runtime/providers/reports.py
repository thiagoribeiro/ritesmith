"""report.* host functions — generate and write self-contained HTML reports.

Reports are stored at:
    {RITESMITH_REPORTS_PATH}/{YYYYMM}/{theme}/{YYYYMMDD_HHMMSS}_{description}.html

HTML shell includes Vue.js + Vuetify + Chart.js via CDN (dark theme by default).
After writing, sends the report path via Telegram (best-effort, does not fail the write).
"""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime
from pathlib import Path

import httpx

from ritesmith.config import get_settings
from ritesmith.runtime._conversion import lua_to_python
from ritesmith.runtime.providers.base import HostFunctionDef, MCPToolDef, ToolProvider

logger = logging.getLogger(__name__)

_TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"
_TELEGRAM_TIMEOUT = 5.0

_HTML_TEMPLATE = """\
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>{title}</title>
  <link href="https://fonts.googleapis.com/css?family=Roboto:100,300,400,500,700,900" rel="stylesheet"/>
  <link href="https://cdn.jsdelivr.net/npm/@mdi/font@7/css/materialdesignicons.min.css" rel="stylesheet"/>
  <link href="https://cdn.jsdelivr.net/npm/vuetify@3/dist/vuetify.min.css" rel="stylesheet"/>
  <script src="https://cdn.jsdelivr.net/npm/chart.js@4/dist/chart.umd.min.js"></script>
  <style>[v-cloak]{{display:none}} body{{background:#121212}}</style>
</head>
<body>
  <div id="app" v-cloak>
{body}
  </div>
  <script src="https://cdn.jsdelivr.net/npm/vue@3/dist/vue.global.prod.js"></script>
  <script src="https://cdn.jsdelivr.net/npm/vuetify@3/dist/vuetify.min.js"></script>
  <script>
    const {{createApp}} = Vue, {{createVuetify}} = Vuetify;
    const vuetify = createVuetify({{theme: {{defaultTheme: 'dark'}}}});
    createApp({{}}).use(vuetify).mount('#app');
    {chart_inits}
  </script>
</body>
</html>"""


def _sanitize_slug(value: str, max_len: int) -> str:
    slug = re.sub(r"[^a-z0-9_-]", "", value.lower().replace(" ", "_"))
    return slug[:max_len]


def _reports_root() -> Path | None:
    p = get_settings().reports_path
    return Path(p).resolve() if p else None


def _compute_path(theme: str, description: str) -> Path:
    root = _reports_root()
    assert root is not None
    now = datetime.now(UTC)
    month_dir = now.strftime("%Y%m")
    timestamp = now.strftime("%Y%m%d_%H%M%S")
    filename = f"{timestamp}_{description}.html"
    return root / month_dir / theme / filename


def _build_html(title: str, body: str, charts: dict | None) -> str:
    chart_inits = ""
    if charts:
        chart_inits = "\n    ".join(
            f"new Chart(document.getElementById({json.dumps(cid)}), {json.dumps(cfg, default=str)});"
            for cid, cfg in charts.items()
        )
    return _HTML_TEMPLATE.format(
        title=title,
        body=body,
        chart_inits=chart_inits,
    )


def _send_telegram(title: str, path: str) -> None:
    settings = get_settings()
    token = settings.telegram_bot_token
    chat_id = settings.telegram_chat_id
    if not token or not chat_id:
        return
    try:
        with httpx.Client(timeout=_TELEGRAM_TIMEOUT) as client:
            client.post(
                _TELEGRAM_API.format(token=token),
                json={"chat_id": chat_id, "text": f"📊 {title}\n📁 {path}"},
            )
    except Exception as e:
        logger.warning("Telegram notification failed: %s", e)


def _write_html(args) -> dict:
    """Write a self-contained HTML report and notify via Telegram."""
    if hasattr(args, "items"):
        args = lua_to_python(args)

    theme_raw = str(args.get("theme") or "misc")
    desc_raw = str(args.get("description") or args.get("title") or "report")
    title = str(args.get("title") or "Report")
    # accept "body" or "html" as the markup field
    body = str(args.get("body") or args.get("html") or "")
    charts_raw = args.get("charts")

    root = _reports_root()
    if root is None:
        return {"path": None, "error": "RITESMITH_REPORTS_PATH is not configured"}

    theme = _sanitize_slug(theme_raw, 30)
    if not theme:
        return {"path": None, "error": f"invalid theme: {theme_raw!r}"}

    description = _sanitize_slug(desc_raw, 60)
    if not description:
        return {"path": None, "error": f"invalid description: {desc_raw!r}"}

    charts: dict | None = None
    if charts_raw is not None:
        converted = lua_to_python(charts_raw) if hasattr(charts_raw, "items") else charts_raw
        if isinstance(converted, dict):
            charts = converted

    full_path = _compute_path(theme, description)

    # Path escape guard
    try:
        resolved = full_path.resolve()
        if not str(resolved).startswith(str(root)):
            return {"path": None, "error": "Path outside reports directory"}
    except Exception as e:
        return {"path": None, "error": f"Path error: {e}"}

    try:
        full_path.parent.mkdir(parents=True, exist_ok=True)
        html = _build_html(title, body, charts)
        full_path.write_text(html, encoding="utf-8")
    except Exception as e:
        return {"path": None, "error": f"Write failed: {e}"}

    path_str = str(full_path)
    _send_telegram(title, path_str)
    return {"path": path_str, "error": None}


def _path_for(args) -> dict:
    """Return the path a report would be written to without creating the file."""
    if hasattr(args, "items"):
        args = lua_to_python(args)

    theme = _sanitize_slug(str(args.get("theme") or ""), 30)
    description = _sanitize_slug(str(args.get("description") or ""), 60)

    root = _reports_root()
    if root is None:
        return {"path": None, "error": "RITESMITH_REPORTS_PATH is not configured"}
    if not theme:
        return {"path": None, "error": "invalid theme"}
    if not description:
        return {"path": None, "error": "invalid description"}

    return {"path": str(_compute_path(theme, description)), "error": None}


def _list_reports(theme: str = "", limit: int = 20) -> list[dict]:
    root = _reports_root()
    if root is None:
        return [{"error": "RITESMITH_REPORTS_PATH is not configured"}]
    try:
        pattern = f"**/{theme}/**/*.html" if theme else "**/*.html"
        files = sorted(root.glob(pattern), key=lambda p: p.stat().st_mtime, reverse=True)
        result = []
        for f in files[:limit]:
            parts = f.relative_to(root).parts
            file_theme = parts[1] if len(parts) >= 3 else ""
            stem = f.stem
            # stem is YYYYMMDD_HHMMSS_description — extract description after second _
            description = "_".join(stem.split("_")[2:]) if stem.count("_") >= 2 else stem
            result.append(
                {
                    "path": str(f),
                    "theme": file_theme,
                    "description": description,
                    "created_at": datetime.fromtimestamp(f.stat().st_mtime, tz=UTC).isoformat(),
                }
            )
        return result
    except Exception as e:
        return [{"error": str(e)}]


class ReportsProvider(ToolProvider):
    namespace = "report"
    profile = "filesystem_write"
    risk_level = "medium"
    side_effects = "filesystem_write"

    def is_available(self) -> bool:
        return _reports_root() is not None

    def lua_functions(self) -> dict[str, HostFunctionDef]:
        return {
            "report.write_html": HostFunctionDef(
                name="report.write_html",
                profile="filesystem_write",
                callable=_write_html,
                description=(
                    "Write a self-contained HTML report (Vue.js + Vuetify + Chart.js dark theme). "
                    "Pass charts={canvas_id: Chart.js_config} to add Chart.js charts. "
                    "Sends the report path via Telegram after writing."
                ),
                input_schema={
                    "type": "object",
                    "required": ["theme", "description", "title", "body"],
                    "properties": {
                        "theme": {
                            "type": "string",
                            "description": "Topic folder, e.g. market, pokemon, email",
                        },
                        "description": {
                            "type": "string",
                            "description": "Short filename description",
                        },
                        "title": {"type": "string", "description": "HTML page title"},
                        "body": {
                            "type": "string",
                            "description": "Vuetify markup for <div id=app>",
                        },
                        "charts": {
                            "type": "object",
                            "description": "canvas_id → Chart.js config objects",
                        },
                    },
                },
                output_schema={
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "error": {"type": ["string", "null"]},
                    },
                },
            ),
            "report.path_for": HostFunctionDef(
                name="report.path_for",
                profile="filesystem_write",
                callable=_path_for,
                description="Return the filesystem path a report would be written to, without creating the file.",
                input_schema={
                    "type": "object",
                    "required": ["theme", "description"],
                    "properties": {
                        "theme": {"type": "string"},
                        "description": {"type": "string"},
                    },
                },
                output_schema={
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "error": {"type": ["string", "null"]},
                    },
                },
            ),
            "report.list_reports": HostFunctionDef(
                name="report.list_reports",
                profile="filesystem_write",
                callable=_list_reports,
                description="List existing reports in REPORTS_PATH, sorted by creation date (newest first).",
                input_schema={
                    "type": "object",
                    "properties": {
                        "theme": {"type": "string", "default": ""},
                        "limit": {"type": "integer", "default": 20},
                    },
                },
                output_schema={"type": "array"},
            ),
        }

    def mcp_tools(self) -> list[MCPToolDef]:
        import asyncio

        async def _mcp_list(theme: str = "", limit: int = 20, **_) -> list[dict]:
            return await asyncio.to_thread(_list_reports, theme, limit)

        async def _mcp_read(path: str, **_) -> str:
            root = _reports_root()
            if root is None:
                return "[error] RITESMITH_REPORTS_PATH is not configured"
            try:
                resolved = Path(path).resolve()
                if not str(resolved).startswith(str(root)):
                    return "[error] Path outside reports directory"
                return resolved.read_text(encoding="utf-8")
            except FileNotFoundError:
                return f"[error] Report not found: {path}"
            except Exception as e:
                return f"[error] {e}"

        return [
            MCPToolDef(
                name="report_list",
                description="List HTML reports stored in RiteSmith's reports directory, newest first.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "theme": {
                            "type": "string",
                            "default": "",
                            "description": "Filter by theme (e.g. market, email)",
                        },
                        "limit": {"type": "integer", "default": 20, "maximum": 100},
                    },
                },
                fn=_mcp_list,
            ),
            MCPToolDef(
                name="report_read",
                description="Read the raw HTML content of a RiteSmith report by its filesystem path.",
                input_schema={
                    "type": "object",
                    "required": ["path"],
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "Absolute path returned by report_list",
                        }
                    },
                },
                fn=_mcp_read,
            ),
        ]
