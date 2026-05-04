from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="RITESMITH_", env_file=".env", extra="ignore")

    # HTTP
    http_host: str = "0.0.0.0"
    http_port: int = 8081

    # Database
    database_url: str = "postgresql+asyncpg://ritesmith:ritesmith@localhost:5432/ritesmith"

    # LLM
    llm_provider: str = "openai"
    llm_model: str = "gpt-4.1"
    llm_model_fast: str = "gpt-4o-mini"
    llm_timeout_seconds: int = 60

    # Embeddings (deferred to V1)
    embeddings_enabled: bool = False

    # Lua runtime
    lua_enabled: bool = True
    lua_timeout_ms: int = 1000
    lua_memory_limit_mb: int = 32
    lua_sandbox_workers: int = 8

    # Generation
    generation_max_attempts: int = 5

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
    web_search_api_key: str | None = None         # RITESMITH_WEB_SEARCH_API_KEY (Brave)
    exa_api_key: str | None = None                # RITESMITH_EXA_API_KEY

    # Telegram
    telegram_bot_token: str | None = None         # RITESMITH_TELEGRAM_BOT_TOKEN
    telegram_chat_id: str | None = None           # RITESMITH_TELEGRAM_CHAT_ID

    # Google (OAuth2 — one token.json covers both Calendar and Gmail)
    google_token_json: str | None = None          # RITESMITH_GOOGLE_TOKEN_JSON (path)
    google_client_secrets_json: str | None = None # RITESMITH_GOOGLE_CLIENT_SECRETS_JSON
    google_calendar_ids: str | None = None        # RITESMITH_GOOGLE_CALENDAR_IDS e.g. "personal:primary,family:id@..."

    # DuckDB
    duckdb_path: str | None = None                # RITESMITH_DUCKDB_PATH
    duckdb_allowed_paths: str = ""                # RITESMITH_DUCKDB_ALLOWED_PATHS (comma-separated)

    # Obsidian vault
    obsidian_vault_path: str | None = None        # RITESMITH_OBSIDIAN_VAULT_PATH

    # Reports
    reports_path: str | None = None               # RITESMITH_REPORTS_PATH (e.g. /home/thiago/reports)

    # Trama integration
    public_url: str | None = None                 # RITESMITH_PUBLIC_URL (e.g. http://ritesmith:8081)
    trama_token: str | None = None                # RITESMITH_TRAMA_TOKEN (shared secret for /trama/execute)


@lru_cache
def get_settings() -> Settings:
    return Settings()
