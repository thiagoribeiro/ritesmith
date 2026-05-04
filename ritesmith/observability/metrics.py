"""Single source of truth for all Prometheus instruments.

Import individual metrics from here — never create Counter/Histogram elsewhere.
"""
from prometheus_client import Counter, Gauge, Histogram

_BUCKETS_LATENCY = (0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0)
_BUCKETS_SMALL   = (0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5)
_BUCKETS_COUNT   = (1, 2, 5, 10, 20, 50, 100, 500)
_BUCKETS_DEPTH   = (1, 2, 4, 8, 16, 32)

# ── HTTP ─────────────────────────────────────────────────────────────────────
http_requests_total = Counter(
    "ritesmith_http_requests_total",
    "Total HTTP requests",
    ["method", "endpoint", "status_code"],
)
http_request_duration = Histogram(
    "ritesmith_http_request_duration_seconds",
    "HTTP request duration",
    ["method", "endpoint"],
    buckets=_BUCKETS_LATENCY,
)

# ── LLM ──────────────────────────────────────────────────────────────────────
llm_request_duration = Histogram(
    "ritesmith_llm_request_duration_seconds",
    "LLM API call duration",
    ["provider", "method"],
    buckets=_BUCKETS_LATENCY,
)
llm_tokens_total = Counter(
    "ritesmith_llm_tokens_total",
    "LLM tokens consumed",
    ["provider", "method", "token_type"],
)
llm_errors_total = Counter(
    "ritesmith_llm_errors_total",
    "LLM API errors",
    ["provider", "error_type"],
)

# ── Generation ───────────────────────────────────────────────────────────────
generation_attempts_total = Counter(
    "ritesmith_generation_attempts_total",
    "Generation loop attempts",
    ["artifact_type", "outcome"],   # outcome: success | exhausted
)
artifact_reuse_total = Counter(
    "ritesmith_artifact_reuse_total",
    "Artifact reuse hits from FTS",
    ["artifact_type"],
)
validation_failures_total = Counter(
    "ritesmith_validation_failures_total",
    "Validation check failures",
    ["artifact_type", "check_name"],
)

# ── Execution ─────────────────────────────────────────────────────────────────
execution_duration = Histogram(
    "ritesmith_execution_duration_seconds",
    "End-to-end execution duration",
    ["runtime"],   # lua | trama
    buckets=_BUCKETS_LATENCY,
)
execution_status_total = Counter(
    "ritesmith_execution_status_total",
    "Execution terminal status counts",
    ["status"],
)
lua_timeout_total = Counter(
    "ritesmith_lua_timeout_total",
    "Lua executions that hit the timeout",
)

# ── FTS / DB ──────────────────────────────────────────────────────────────────
fts_search_duration = Histogram(
    "ritesmith_fts_search_duration_seconds",
    "Full-text search query duration",
    ["artifact_type"],
    buckets=_BUCKETS_SMALL,
)
fts_search_results = Histogram(
    "ritesmith_fts_search_result_count",
    "Number of results returned by FTS",
    buckets=_BUCKETS_COUNT,
)

# ── Policy ───────────────────────────────────────────────────────────────────
policy_decisions_total = Counter(
    "ritesmith_policy_decisions_total",
    "Policy evaluation outcomes",
    ["decision", "rule"],
)

# ── Audit ────────────────────────────────────────────────────────────────────
audit_write_failures_total = Counter(
    "ritesmith_audit_write_failures_total",
    "Audit events silently lost due to DB write failure",
    ["event_type"],
)

# ── Sandbox ──────────────────────────────────────────────────────────────────
sandbox_queue_depth = Gauge(
    "ritesmith_sandbox_queue_depth",
    "Number of Lua scripts waiting in ThreadPoolExecutor queue",
)
sandbox_conversion_depth = Histogram(
    "ritesmith_sandbox_conversion_depth",
    "Max recursion depth reached during Lua↔Python table conversion",
    buckets=_BUCKETS_DEPTH,
)
