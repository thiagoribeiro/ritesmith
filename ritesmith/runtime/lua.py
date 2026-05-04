import asyncio
import time

import jsonschema

from ritesmith.config import Settings
from ritesmith.runtime.base import BaseRuntime, RuntimeResult
from ritesmith.runtime.sandbox import run_in_sandbox


class LuaScriptRuntime(BaseRuntime):
    def __init__(self, settings: Settings):
        self.default_timeout_ms = settings.lua_timeout_ms
        self.memory_limit_mb = settings.lua_memory_limit_mb

    async def execute(
        self,
        content: str,
        input_data: dict,
        context: dict,
        timeout_ms: int | None = None,
        profile: str = "transform_only",
        output_schema: dict | None = None,
    ) -> RuntimeResult:
        effective_timeout = timeout_ms if timeout_ms is not None else self.default_timeout_ms
        start = time.monotonic()

        loop = asyncio.get_event_loop()
        output, error, timed_out = await loop.run_in_executor(
            None,
            run_in_sandbox,
            content,
            input_data,
            context,
            profile,
            effective_timeout,
        )

        duration_ms = int((time.monotonic() - start) * 1000)

        if error:
            return RuntimeResult(
                output={},
                error=error,
                duration_ms=duration_ms,
                timed_out=timed_out,
            )

        # Valida output contra schema declarado, se fornecido
        if output_schema and output is not None:
            try:
                jsonschema.validate(instance=output, schema=output_schema)
            except jsonschema.ValidationError as e:
                return RuntimeResult(
                    output=output or {},
                    error=f"Output schema validation failed: {e.message}",
                    duration_ms=duration_ms,
                )

        return RuntimeResult(
            output=output or {},
            duration_ms=duration_ms,
        )

    def validate_syntax(self, content: str) -> list[str]:
        """Tenta carregar o script em Lua e retorna erros de sintaxe."""
        try:
            from lupa import LuaError, LuaRuntime
            lua = LuaRuntime(unpack_returned_tuples=False)
            lua.execute(content)
            return []
        except Exception as e:
            return [str(e)]
