class RiteSmithError(Exception):
    status_code: int = 500
    error_code: str = "internal_error"

    def __init__(self, message: str, details: dict | None = None):
        super().__init__(message)
        self.message = message
        self.details = dict(details) if details else {}

    def __str__(self) -> str:
        return f"[{self.error_code}] {self.message}"


class NotFoundError(RiteSmithError):
    status_code = 404
    error_code = "not_found"


class ConflictError(RiteSmithError):
    status_code = 409
    error_code = "conflict"


class ValidationFailedError(RiteSmithError):
    status_code = 400
    error_code = "validation_failed"


class PolicyDeniedError(RiteSmithError):
    status_code = 403
    error_code = "policy_denied"


class GenerationFailedError(RiteSmithError):
    status_code = 422
    error_code = "generation_failed"


class UnsupportedRuntimeError(RiteSmithError):
    status_code = 400
    error_code = "unsupported_runtime"


class InvalidTransitionError(RiteSmithError):
    status_code = 409
    error_code = "invalid_transition"


class LLMError(RiteSmithError):
    status_code = 502
    error_code = "llm_error"


class LLMTimeoutError(LLMError):
    error_code = "llm_timeout"


class LLMRateLimitError(LLMError):
    error_code = "llm_rate_limit"
