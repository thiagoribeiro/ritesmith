"""duckdb.* host functions — read-only queries on a DuckDB database file.

The connection is opened read-only. Results are capped at 500 rows.
`query_file` is only allowed for paths under RITESMITH_DUCKDB_ALLOWED_PATHS.
"""
from __future__ import annotations

import logging
import os

from ritesmith.config import get_settings
from ritesmith.runtime.providers.base import HostFunctionDef, MCPToolDef, ToolProvider

logger = logging.getLogger(__name__)

_MAX_ROWS = 500


def _run_query(sql: str, db_path: str) -> list[dict]:
    import duckdb
    try:
        con = duckdb.connect(db_path, read_only=True)
        rel = con.execute(sql)
        cols = [d[0] for d in rel.description]
        rows = rel.fetchmany(_MAX_ROWS)
        con.close()
        return [dict(zip(cols, row)) for row in rows]
    except Exception as e:
        logger.warning("duckdb query failed: %s", e)
        return [{"error": str(e)}]


def _query(sql: str) -> list[dict]:
    db_path = get_settings().duckdb_path
    if not db_path:
        return [{"error": "RITESMITH_DUCKDB_PATH not configured"}]
    return _run_query(sql, db_path)


def _query_file(path: str, sql: str) -> list[dict]:
    allowed_raw = get_settings().duckdb_allowed_paths
    if not allowed_raw:
        return [{"error": "RITESMITH_DUCKDB_ALLOWED_PATHS not configured"}]
    allowed = [p.strip() for p in allowed_raw.split(",") if p.strip()]
    abs_path = os.path.realpath(path)
    if not any(abs_path.startswith(os.path.realpath(a)) for a in allowed):
        return [{"error": f"Path not in allowed list: {path}"}]
    return _run_query(sql, abs_path)


class DuckdbProvider(ToolProvider):
    namespace = "duckdb"
    profile = "analytics_local"
    risk_level = "low"
    side_effects = "none"

    def is_available(self) -> bool:
        if not get_settings().duckdb_path:
            return False
        try:
            import duckdb  # noqa: F401
            return True
        except ImportError:
            return False

    def lua_functions(self) -> dict[str, HostFunctionDef]:
        _query_output = {"type": "object", "properties": {"rows": {"type": "array"}, "columns": {"type": "array"}}}
        return {
            "duckdb.query": HostFunctionDef(
                "duckdb.query", "analytics_local", _query,
                description="Run a read-only SQL query on the configured DuckDB database. Returns rows and column names.",
                input_schema={"type": "object", "required": ["sql"], "properties": {
                    "sql": {"type": "string", "description": "SELECT query"},
                }},
                output_schema=_query_output,
            ),
            "duckdb.query_file": HostFunctionDef(
                "duckdb.query_file", "analytics_local", _query_file,
                description="Run a read-only SQL query on an alternate allowed DuckDB file.",
                input_schema={"type": "object", "required": ["sql", "path"], "properties": {
                    "sql": {"type": "string"}, "path": {"type": "string"},
                }},
                output_schema=_query_output,
            ),
        }

    def mcp_tools(self) -> list[MCPToolDef]:
        import asyncio

        async def _mcp_query(sql: str, file_path: str | None = None, **_) -> list[dict]:
            if file_path:
                return await asyncio.to_thread(_query_file, file_path, sql)
            return await asyncio.to_thread(_query, sql)

        return [
            MCPToolDef(
                name="duckdb_query",
                description=(
                    "Run a read-only SQL query on the configured DuckDB database. "
                    "Optionally specify file_path to query a different DuckDB file "
                    "(must be under RITESMITH_DUCKDB_ALLOWED_PATHS). Returns up to 500 rows."
                ),
                input_schema={
                    "type": "object",
                    "required": ["sql"],
                    "properties": {
                        "sql": {"type": "string", "description": "SQL query to execute"},
                        "file_path": {"type": "string", "description": "Path to a .duckdb file (optional)"},
                    },
                },
                fn=_mcp_query,
            ),
        ]
