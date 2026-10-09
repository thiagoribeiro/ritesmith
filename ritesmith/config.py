from functools import lru_cache
from typing import Literal

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class CASPProviderConfig(BaseModel):
    url: str
    resource_types: list[str] = []  # empty = handles all types
    weight: int = 100
    priority: int = 0  # lower = tried first


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="RITESMITH_", env_file=".env", extra="ignore", env_parse_none_str="null"
    )

    # HTTP
    http_host: str = "0.0.0.0"
    http_port: int = 8081

    # Database
    database_url: str = "postgresql+asyncpg://ritesmith:ritesmith@localhost:5432/ritesmith"

    # LLM
    # Two tiers: llm_model does the hard generation (workflow/lua, repairs);
    # llm_model_fast does classification and short reviews (analyze_intent,
    # llm.evaluate). Keep llm_model_fast non-reasoning — reasoning models spend
    # their whole token budget before emitting the JSON these call sites parse.
    llm_provider: str = "openai"
    llm_model: str = "gpt-5-mini"
    llm_model_fast: str = "gpt-4.1-nano"
    llm_script_model: str | None = None
    llm_workflow_model: str | None = None
    llm_reasoning_effort: Literal["minimal", "none", "low", "medium", "high"] | None = "low"
    llm_script_effort: Literal["minimal", "none", "low", "medium", "high"] | None = None
    llm_workflow_effort: Literal["minimal", "none", "low", "medium", "high"] | None = None
    llm_fallback_enabled: bool = True
    generation_specialized_prompts: bool = True
    generation_compact_workflows: bool = True
    generation_mermaid_workflows: bool = False
    generation_unified_proposal: bool = True
    generation_parallel_tests: bool = True
    generation_avoid_speculative_workflow_tests: bool = True
    generation_latency_target_seconds: float = Field(default=10, gt=0)
    generation_typed_prompts: bool = True
    generation_luau_assembly: bool = False
    generation_semantic_workflows: bool = False
    # Deprecated compatibility setting; it no longer selects JSON rendering behavior.
    generation_workflow_typed_json: bool = False
    generation_structured_outputs: bool = False
    generation_structured_output_operations: list[str] | None = None
    generation_pattern_parameters: bool = False
    generation_pattern_parameter_operations: list[str] | None = None
    generation_bounded_recovery: bool = True
    generation_deadline_seconds: float = Field(default=30, gt=0)
    generation_recovery_min_seconds: float = Field(default=3, ge=0)
    generation_recovery_attempts: int = Field(default=1, ge=0, le=4)
    llm_timeout_seconds: int = 180  # gpt-5* reasoning + workflow JSON regularly exceeds 60s

    # Embeddings (deferred to V1)
    embeddings_enabled: bool = False

    # Script language for newly generated scripts: "luau" (LunarDyson) or "lua" (lupa).
    # Existing artifacts always run on the runtime of their own artifact_type. "luau"
    # falls back to "lua" when the lunardyson package is not installed.
    script_language: Literal["lua", "luau"] = "luau"
    # When true, a program that only fails the strict Luau type check after the repair
    # budget is a generation FAILURE. When false (default), it is persisted as
    # certification="nonstrict" and the PolicyEngine requires approval to run it.
    require_strict_typecheck: bool = False
    # When true, a requested script_language="luau" with the lunardyson package
    # missing is a hard startup failure instead of a silent fallback to "lua" —
    # set this in production so the isolation guarantee never changes unnoticed.
    require_luau: bool = False

    # Lua runtime (lua_timeout_ms / lua_memory_limit_mb also bound Luau executions)
    lua_enabled: bool = True
    lua_timeout_ms: int = 1000
    lua_memory_limit_mb: int = 32
    lua_sandbox_workers: int = 8

    # Luau budgets beyond the VM CPU deadline (lua_timeout_ms). The VM deadline does
    # not interrupt a blocked Python tool call (the VM is not running during it), so:
    #   - luau_tool_timeout_ms caps each individual host-tool call (0 disables);
    #   - luau_wall_clock_ms is a whole-execution wall-clock backstop that abandons
    #     the run if exceeded (0 disables). Set it above VM + summed tool timeouts.
    luau_tool_timeout_ms: int = 5000
    luau_wall_clock_ms: int = 0

    # Generation
    generation_max_attempts: int = 2

    # Test gate: artifacts at/above this risk level must pass executed test cases
    # (caller-supplied, else LLM-generated as a sanity gate). "" / "off" disables.
    require_tests_min_risk: str = "medium"

    # Reuse (three-stage: FTS recall → deterministic contract compat → LLM judge)
    reuse_recall_limit: int = 10
    # When true (default), an LLM makes the final relevance call among compatible
    # candidates. When false, reuse runs deterministic-only (best compatible FTS hit).
    reuse_llm_judge: bool = True

    # Policy
    policy_default: str = "deny"
    allow_unapproved_low_risk: bool = True
    shell_generation_enabled: bool = False

    # Workflow delegation
    workflow_delegation_enabled: bool = False
    workflow_engine_url: str | None = None

    # Observability
    otel_enabled: bool = False

    # Web search
    web_search_api_key: str | None = None  # RITESMITH_WEB_SEARCH_API_KEY (Brave)
    exa_api_key: str | None = None  # RITESMITH_EXA_API_KEY

    # Telegram
    telegram_bot_token: str | None = None  # RITESMITH_TELEGRAM_BOT_TOKEN
    telegram_chat_id: str | None = None  # RITESMITH_TELEGRAM_CHAT_ID

    # Google (OAuth2 — one token.json covers both Calendar and Gmail)
    google_token_json: str | None = None  # RITESMITH_GOOGLE_TOKEN_JSON (path)
    google_client_secrets_json: str | None = None  # RITESMITH_GOOGLE_CLIENT_SECRETS_JSON
    google_calendar_ids: str | None = (
        None  # RITESMITH_GOOGLE_CALENDAR_IDS e.g. "personal:primary,family:id@..."
    )

    # DuckDB
    duckdb_path: str | None = None  # RITESMITH_DUCKDB_PATH
    duckdb_allowed_paths: str = ""  # RITESMITH_DUCKDB_ALLOWED_PATHS (comma-separated)

    # Obsidian vault
    obsidian_vault_path: str | None = None  # RITESMITH_OBSIDIAN_VAULT_PATH

    # Artifact lifecycle
    artifact_ttl_days: int = 30  # RITESMITH_ARTIFACT_TTL_DAYS (0 = no TTL)

    # Reports
    reports_path: str | None = None  # RITESMITH_REPORTS_PATH (e.g. /path/to/reports)

    # Trama integration
    public_url: str | None = None  # RITESMITH_PUBLIC_URL (e.g. http://ritesmith:8081)
    trama_token: str | None = None  # RITESMITH_TRAMA_TOKEN (shared secret for /trama/execute)
    # Comma-separated hosts a plan callback_url may target even though they are
    # private (e.g. the LAN address of the agent that created the plan).
    callback_allowed_hosts: str = ""  # RITESMITH_CALLBACK_ALLOWED_HOSTS
    # Bearer token required on the API (except /health, /metrics and /trama/execute).
    # Unset = API open (development only; a warning is logged at startup).
    api_token: str | None = None  # RITESMITH_API_TOKEN
    # Secret for server-issued approval tokens (falls back to api_token, then trama_token).
    approval_secret: str | None = None  # RITESMITH_APPROVAL_SECRET

    # LoomHarbor integration (legacy home.* provider)
    loomharbor_url: str | None = None  # RITESMITH_LOOMHARBOR_URL (e.g. http://loomharbor:8000)
    loomharbor_secret: str | None = (
        None  # RITESMITH_LOOMHARBOR_SECRET (Bearer token for LoomHarbor API)
    )

    # CASP providers — JSON array of CASPProviderConfig objects
    # e.g. RITESMITH_CASP_PROVIDERS='[{"url":"http://loomharbor:8000"}]'
    casp_providers: list[CASPProviderConfig] = []

    # CASP re-planning
    casp_max_replan_attempts: int = 2

    # Grafana + Prometheus
    grafana_url: str = "http://localhost:3000"  # RITESMITH_GRAFANA_URL
    grafana_token: str | None = None  # RITESMITH_GRAFANA_TOKEN (service account token)
    prometheus_url: str = "http://localhost:9090"  # RITESMITH_PROMETHEUS_URL


@lru_cache
def get_settings() -> Settings:
    return Settings()
