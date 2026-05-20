"""Tool provider registry.

PROVIDERS is the single source of truth for all available tool providers.
Import from here in host_functions.py (Lua sandbox) and in the MCP server.
"""
from ritesmith.runtime.providers.base import HostFunctionDef, MCPToolDef, ToolProvider
from ritesmith.runtime.providers.calendar import CalendarProvider
from ritesmith.runtime.providers.casp import CASPProvider
from ritesmith.runtime.providers.context import ContextProvider
from ritesmith.runtime.providers.duckdb_ import DuckdbProvider
from ritesmith.runtime.providers.email import EmailProvider
from ritesmith.runtime.providers.grafana import GrafanaProvider
from ritesmith.runtime.providers.loomharbor import LoomHarborProvider
from ritesmith.runtime.providers.market import MarketProvider
from ritesmith.runtime.providers.obsidian import ObsidianProvider
from ritesmith.runtime.providers.reports import ReportsProvider
from ritesmith.runtime.providers.stat import StatProvider
from ritesmith.runtime.providers.telegram import TelegramProvider
from ritesmith.runtime.providers.web import WebProvider

PROVIDERS: list[ToolProvider] = [
    StatProvider(),
    ContextProvider(),
    WebProvider(),
    MarketProvider(),
    TelegramProvider(),
    DuckdbProvider(),
    ObsidianProvider(),
    CalendarProvider(),
    EmailProvider(),
    ReportsProvider(),
    CASPProvider(),
    LoomHarborProvider(),
    GrafanaProvider(),
]

__all__ = [
    "ToolProvider",
    "HostFunctionDef",
    "MCPToolDef",
    "PROVIDERS",
]
