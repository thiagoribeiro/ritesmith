"""ScriptedLLM: a deterministic LLMProvider for tests.

Queue the responses each method should return, in order; every call is recorded
in `calls`. Calling a method with an empty queue fails the test loudly instead of
silently returning something.

    llm = ScriptedLLM(supports_luau=True)
    llm.queue("generate_luau", luau_response(script))
    llm.queue("repair_luau", repair_response(fixed))
"""

from collections import defaultdict, deque

from ritesmith.llm.base import (
    IntentAnalysis,
    LLMCallStats,
    LLMProvider,
    LuaGenerationResponse,
    RepairResponse,
    WorkflowGenerationResponse,
    WorkflowRepairResponse,
)


def stats(model: str = "scripted") -> LLMCallStats:
    return LLMCallStats(model=model, prompt_tokens=10, completion_tokens=50, total_tokens=60)


def script_response(script: str, name: str = "scripted_script", **kw) -> LuaGenerationResponse:
    return LuaGenerationResponse(
        script=script, name=name, description=kw.pop("description", name), **kw
    )


def repair_response(content: str, changes: str = "fixed") -> RepairResponse:
    return RepairResponse(repaired_content=content, changes_made=changes)


def workflow_response(
    definition: dict, name: str = "scripted_workflow"
) -> WorkflowGenerationResponse:
    return WorkflowGenerationResponse(definition=definition, name=name, description=name)


def workflow_repair_response(definition: dict) -> WorkflowRepairResponse:
    return WorkflowRepairResponse(definition=definition, changes_made="fixed")


def intent(**kw) -> IntentAnalysis:
    return IntentAnalysis(**kw)


class UnexpectedLLMCall(AssertionError):
    pass


class ScriptedLLM(LLMProvider):
    def __init__(self, supports_luau: bool = False):
        self.supports_luau = supports_luau
        self.calls: list[tuple[str, dict]] = []
        self._queues: dict[str, deque] = defaultdict(deque)

    def queue(self, method: str, *responses) -> "ScriptedLLM":
        self._queues[method].extend(responses)
        return self

    def called(self, method: str) -> list[dict]:
        return [kwargs for name, kwargs in self.calls if name == method]

    def _next(self, method: str, kwargs: dict):
        self.calls.append((method, kwargs))
        if not self._queues[method]:
            raise UnexpectedLLMCall(f"unexpected {method}() call (nothing queued)")
        response = self._queues[method].popleft()
        if isinstance(response, BaseException):
            raise response
        return response, stats()

    async def generate_lua(self, **kwargs):
        return self._next("generate_lua", kwargs)

    async def repair_lua(self, **kwargs):
        return self._next("repair_lua", kwargs)

    async def generate_luau(self, **kwargs):
        return self._next("generate_luau", kwargs)

    async def repair_luau(self, **kwargs):
        return self._next("repair_luau", kwargs)

    async def analyze_intent(
        self, goal: str = "", constraints: dict | None = None, context: dict | None = None
    ):
        return self._next(
            "analyze_intent", {"goal": goal, "constraints": constraints, "context": context}
        )

    async def generate_workflow(self, **kwargs):
        return self._next("generate_workflow", kwargs)

    async def repair_workflow(self, **kwargs):
        return self._next("repair_workflow", kwargs)
