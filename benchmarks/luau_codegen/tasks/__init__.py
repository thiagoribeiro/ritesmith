"""Benchmark task set. Each task carries a hand-written Luau `reference` solution
that `selftest.py` runs to prove the task, stubs and expectations are consistent."""

from benchmarks.luau_codegen.tasks.t1_transform import TASKS as T1
from benchmarks.luau_codegen.tasks.t2_single_tool import TASKS as T2
from benchmarks.luau_codegen.tasks.t3_orchestration import TASKS as T3
from benchmarks.luau_codegen.tasks.t4_heldout import TASKS as T4

for _tier, _tasks in (("T1", T1), ("T2", T2), ("T3", T3), ("T4", T4)):
    for _task in _tasks:
        _task["tier"] = _tier
        # T4 is the generalisation set: never used to tune the prompts.
        _task["held_out"] = _tier == "T4"

ALL_TASKS: list[dict] = [*T1, *T2, *T3, *T4]
TASKS_BY_ID: dict[str, dict] = {t["id"]: t for t in ALL_TASKS}
