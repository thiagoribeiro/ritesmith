from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class RuntimeResult:
    output: dict
    error: str | None = None
    duration_ms: int = 0
    memory_used_bytes: int | None = None
    timed_out: bool = False


class BaseRuntime(ABC):
    @abstractmethod
    async def execute(
        self,
        content: str,
        input_data: dict,
        context: dict,
        timeout_ms: int | None = None,
    ) -> RuntimeResult: ...

    @abstractmethod
    def validate_syntax(self, content: str) -> list[str]:
        """Returns list of syntax error messages (empty = valid)."""
        ...
