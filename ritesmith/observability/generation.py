"""Request-scoped generation accounting shared by API and benchmark."""

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from time import perf_counter

from ritesmith.observability.metrics import generation_stage_duration


@dataclass
class GenerationTrace:
    calls: list[dict] = field(default_factory=list)
    stages: list[dict] = field(default_factory=list)
    artifact_types: set[str] = field(default_factory=set)
    accepted: bool = True
    fallback: bool = False
    recalls: list[tuple] = field(default_factory=list)
    diagnostics: list[dict] = field(default_factory=list)
    client_tests: bool = False
    has_dependencies: bool = False


current_trace: ContextVar[GenerationTrace | None] = ContextVar("generation_trace", default=None)


@contextmanager
def stage(name: str, artifact_type: str):
    start = perf_counter()
    try:
        yield
    finally:
        elapsed = perf_counter() - start
        generation_stage_duration.labels(stage=name, artifact_type=artifact_type).observe(elapsed)
        trace = current_trace.get()
        if trace is not None:
            trace.stages.append(
                {"stage": name, "artifact_type": artifact_type, "duration_s": elapsed}
            )


def outcome(artifact_type: str, accepted: bool):
    trace = current_trace.get()
    if trace is not None:
        trace.artifact_types.add(artifact_type)
        trace.accepted = trace.accepted and accepted
