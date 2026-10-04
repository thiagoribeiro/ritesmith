"""Builders for registry objects and unique strings used across tests."""

import itertools
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from ritesmith.registry.models import GenerationJob
from ritesmith.registry.service import RegistryService

_counter = itertools.count()

LUA_OK = "function run(input, context) return { ok = true } end"
LUAU_OK = (
    "function run(input: { [string]: any }, context: { [string]: any }) return { ok = true } end"
)


def unique(prefix: str = "t") -> str:
    """A token FTS will not match against anything else in the database."""
    return f"{prefix}zq{uuid.uuid4().hex[:10]}x{next(_counter)}"


async def make_artifact(
    db: AsyncSession,
    *,
    artifact_type: str = "lua_script",
    content: str | None = None,
    name: str | None = None,
    description: str | None = None,
    usage_description: str | None = None,
    risk_level: str = "low",
    runtime_profile: str = "transform_only",
    input_schema: dict | None = None,
    output_schema: dict | None = None,
    tags: list[str] | None = None,
    metadata: dict | None = None,
):
    """Creates an artifact (version 1) through RegistryService; returns (artifact, version)."""
    content = (
        content if content is not None else (LUAU_OK if artifact_type == "luau_script" else LUA_OK)
    )
    return await RegistryService(db).create_artifact(
        name=name or unique("artifact"),
        artifact_type=artifact_type,
        content=content,
        description=description or "test artifact",
        usage_description=usage_description,
        tags=tags or [],
        input_schema=input_schema,
        output_schema=output_schema,
        risk_level=risk_level,
        metadata={"runtime_profile": runtime_profile, **(metadata or {})},
    )


async def make_generation_job(
    db: AsyncSession,
    *,
    goal: str,
    final_artifact_id: str | None = None,
    target_type: str = "lua_script",
) -> GenerationJob:
    job = GenerationJob(
        generation_id=f"gen_{uuid.uuid4().hex[:20]}",
        goal=goal,
        target_type=target_type,
        status="completed",
        final_artifact_id=final_artifact_id,
    )
    db.add(job)
    await db.flush()
    return job


async def make_plan(
    db: AsyncSession,
    *,
    status: str = "proposed",
    intent: str | None = None,
    context: dict | None = None,
    callback_url: str | None = None,
    summary: str | None = None,
):
    """Seed a Plan row directly in a chosen status (bypasses the builder)."""
    from ritesmith.registry.models import Plan as PlanORM

    plan = PlanORM(
        plan_id=f"plan_{uuid.uuid4().hex[:20]}",
        status=status,
        intent=intent or unique("plan intent"),
        summary=summary,
        mode="propose",
        reuse_policy="prefer_reuse",
        context=context,
        steps=[],
        artifact_ids=[],
        callback_url=callback_url,
    )
    db.add(plan)
    await db.flush()
    return plan
