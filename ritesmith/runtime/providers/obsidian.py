"""obsidian.* host functions — search and read notes from an Obsidian vault.

Search uses `rg` (ripgrep) if available, falling back to `grep -r`.
All operations are read-only filesystem access.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from pathlib import Path

from ritesmith.config import get_settings
from ritesmith.runtime.providers.base import HostFunctionDef, MCPToolDef, ToolProvider

logger = logging.getLogger(__name__)

_MAX_RESULTS = 20
_EXCERPT_CONTEXT = 150  # chars around match


def _vault() -> str | None:
    return get_settings().obsidian_vault_path


def _search(query: str, limit: int = 10) -> list[dict]:
    vault = _vault()
    if not vault:
        return [{"error": "RITESMITH_OBSIDIAN_VAULT_PATH not configured"}]

    rg = shutil.which("rg")
    grep = shutil.which("grep")

    try:
        if rg:
            cmd = [rg, "--files-with-matches", "--ignore-case", "--glob=*.md", query, vault]
        elif grep:
            cmd = [
                "grep",
                "--recursive",
                "--files-with-matches",
                "--ignore-case",
                "--include=*.md",
                query,
                vault,
            ]
        else:
            return [{"error": "Neither rg nor grep found on PATH"}]

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10, check=False)
        files = [f for f in result.stdout.strip().splitlines() if f][: min(limit, _MAX_RESULTS)]
    except subprocess.TimeoutExpired:
        return [{"error": "Search timed out"}]
    except Exception as e:
        return [{"error": str(e)}]

    results = []
    for filepath in files:
        try:
            content = Path(filepath).read_text(errors="replace")
            lower_content = content.lower()
            lower_query = query.lower()
            idx = lower_content.find(lower_query)
            if idx >= 0:
                start = max(0, idx - _EXCERPT_CONTEXT)
                end = min(len(content), idx + len(query) + _EXCERPT_CONTEXT)
                excerpt = content[start:end].replace("\n", " ").strip()
            else:
                excerpt = content[:300].replace("\n", " ").strip()
            results.append(
                {
                    "title": Path(filepath).stem,
                    "path": os.path.relpath(filepath, vault),
                    "excerpt": excerpt,
                }
            )
        except Exception:
            logger.debug("obsidian search: skipping unreadable file %s", filepath, exc_info=True)
            continue

    return results


def _read(relative_path: str) -> str:
    vault = _vault()
    if not vault:
        return "[error] RITESMITH_OBSIDIAN_VAULT_PATH not configured"
    try:
        full = Path(vault) / relative_path
        resolved = full.resolve()
        vault_resolved = Path(vault).resolve()
        if not str(resolved).startswith(str(vault_resolved)):
            return "[error] Path outside vault"
        return resolved.read_text(errors="replace")
    except FileNotFoundError:
        return f"[error] Note not found: {relative_path}"
    except Exception as e:
        return f"[error] {e}"


def _list_notes(folder: str = "") -> list[dict]:
    vault = _vault()
    if not vault:
        return [{"error": "RITESMITH_OBSIDIAN_VAULT_PATH not configured"}]
    try:
        base = Path(vault) / folder if folder else Path(vault)
        notes = sorted(base.rglob("*.md"))[:_MAX_RESULTS]
        return [{"title": p.stem, "path": os.path.relpath(str(p), vault)} for p in notes]
    except Exception as e:
        return [{"error": str(e)}]


class ObsidianProvider(ToolProvider):
    namespace = "obsidian"
    profile = "analytics_local"
    risk_level = "low"
    side_effects = "none"

    def is_available(self) -> bool:
        vault = _vault()
        return bool(vault and Path(vault).is_dir())

    def lua_functions(self) -> dict[str, HostFunctionDef]:
        return {
            "obsidian.search": HostFunctionDef(
                "obsidian.search",
                "analytics_local",
                _search,
                description="Search the Obsidian vault using ripgrep. Returns matching notes with snippets.",
                input_schema={
                    "type": "object",
                    "required": ["query"],
                    "properties": {
                        "query": {"type": "string"},
                        "limit": {"type": "integer", "default": 10},
                    },
                },
                output_schema={"type": "object", "properties": {"results": {"type": "array"}}},
            ),
            "obsidian.read": HostFunctionDef(
                "obsidian.read",
                "analytics_local",
                _read,
                description="Read the full content of an Obsidian note by its path.",
                input_schema={
                    "type": "object",
                    "required": ["path"],
                    "properties": {
                        "path": {"type": "string"},
                    },
                },
                output_schema={
                    "type": "object",
                    "properties": {"content": {"type": "string"}, "path": {"type": "string"}},
                },
            ),
            "obsidian.list": HostFunctionDef(
                "obsidian.list",
                "analytics_local",
                _list_notes,
                description="List all notes in an Obsidian vault folder.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "folder": {"type": "string", "default": ""},
                    },
                },
                output_schema={"type": "object", "properties": {"notes": {"type": "array"}}},
            ),
        }

    def mcp_tools(self) -> list[MCPToolDef]:
        import asyncio

        async def _mcp_search(query: str, limit: int = 10, **_) -> list[dict]:
            return await asyncio.to_thread(_search, query, limit)

        async def _mcp_read(path: str, **_) -> str:
            return await asyncio.to_thread(_read, path)

        async def _mcp_list(folder: str = "", **_) -> list[dict]:
            return await asyncio.to_thread(_list_notes, folder)

        return [
            MCPToolDef(
                name="obsidian_search",
                description="Search the Obsidian vault for notes matching a query (uses rg/grep). Returns title, relative path, and excerpt.",
                input_schema={
                    "type": "object",
                    "required": ["query"],
                    "properties": {
                        "query": {"type": "string"},
                        "limit": {"type": "integer", "default": 10, "maximum": 20},
                    },
                },
                fn=_mcp_search,
            ),
            MCPToolDef(
                name="obsidian_read",
                description="Read the full markdown content of an Obsidian note by its relative path within the vault.",
                input_schema={
                    "type": "object",
                    "required": ["path"],
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "Relative path from vault root, e.g. 'Projects/Jarvis/Overview.md'",
                        }
                    },
                },
                fn=_mcp_read,
            ),
            MCPToolDef(
                name="obsidian_list",
                description="List notes in the Obsidian vault, optionally filtered to a subfolder.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "folder": {
                            "type": "string",
                            "default": "",
                            "description": "Subfolder path relative to vault root",
                        }
                    },
                },
                fn=_mcp_list,
            ),
        ]
