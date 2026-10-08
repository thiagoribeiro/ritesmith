"""Prepare one request-local recall and one initial proposal for automatic routing."""

import asyncio
import re

from ritesmith.core.exceptions import LLMError
from ritesmith.core.repair import claim_recovery
from ritesmith.core.reuse import check_reuse
from ritesmith.observability.generation import stage
from ritesmith.registry.search import fts_search
from ritesmith.runtime.host_functions import get_available_provider_capabilities
from ritesmith.runtime.luau import effective_script_language


async def prepare_proposal(
    db,
    llm,
    settings,
    *,
    goal,
    constraints,
    context=None,
    input_schema=None,
    output_schema=None,
    profile="transform_only",
    audit=None,
    allow_dependencies=True,
):
    if not settings.generation_unified_proposal or not llm.supports_proposal:
        return None, None, None
    with stage("recall", "mixed"):
        recall = await fts_search(
            db,
            goal,
            artifact_types=["luau_script", "lua_script", "trama_workflow"],
            limit=max(5, settings.reuse_recall_limit),
        )
    reusable = await check_reuse(
        db,
        goal,
        ["luau_script", "lua_script", "trama_workflow"],
        constraints.get("reuse_policy", "prefer_reuse"),
        audit,
        input_schema=input_schema,
        output_schema=output_schema,
        runtime_profile=profile,
        llm=llm,
        settings=settings,
        results=recall,
    )
    if reusable is not None:
        return None, recall, reusable
    from ritesmith.core.workflow_generation import WorkflowGenerationService

    db_caps = await WorkflowGenerationService(db, llm, settings)._get_capabilities()
    kwargs = {
        "goal": goal,
        "constraints": constraints,
        "context": context,
        "input_schema": input_schema,
        "output_schema": output_schema,
        "language": effective_script_language(settings),
        "profile": profile,
        "allow_dependencies": allow_dependencies,
        "available_capabilities": get_available_provider_capabilities() + db_caps,
        "similar_artifacts": [
            {
                "name": r.artifact.name,
                "content": r.version.content if r.version else "",
                "kind": "script"
                if r.artifact.artifact_type in ("lua_script", "luau_script")
                else "workflow",
            }
            for r in recall[:2]
        ],
    }
    tests_task = None
    if (
        settings.generation_parallel_tests
        and settings.require_tests_min_risk not in ("", "off")
        and not (context or {}).get("test_cases")
        and not (
            settings.generation_avoid_speculative_workflow_tests
            and not (input_schema or output_schema)
            and not re.search(
                r"\b(luau|lua|script|function|função|funcao|código|codigo)\b", goal, re.IGNORECASE
            )
            and re.search(
                r"\b(workflow|trama|monitor|watch|every|reminder|minutes?|hours?|days?)\b"
                r"|lembre|monitore|a cada|in parallel|em paralelo|independently",
                goal,
                re.IGNORECASE,
            )
        )
    ):
        tests_task = asyncio.create_task(
            llm.generate_validation_tests(
                goal,
                input_schema,
                output_schema,
                profile=profile,
                fixtures=(context or {}).get("test_fixtures", []),
            )
        )
    try:
        try:
            with stage("proposal", "mixed"):
                proposal, stats = await llm.propose(**kwargs)
        except LLMError as exc:
            if exc.details.get("retryable") is False:
                raise
            fallback = llm.fallback()
            if fallback is llm or not claim_recovery(
                "proposal", settings.generation_recovery_attempts
            ):
                raise
            with stage("proposal_fallback", "mixed"):
                proposal, stats = await fallback.propose(**kwargs)
                stats.api_attempts += exc.details.get("api_attempts", 1)
        if tests_task is not None and proposal.script is not None:
            try:
                proposal._test_cases, _ = await tests_task
                proposal._tests_attempted = True
            except NotImplementedError:
                proposal._tests_attempted = True
    finally:
        if tests_task is not None:
            if not tests_task.done():
                tests_task.cancel()
            await asyncio.gather(tests_task, return_exceptions=True)
    return (proposal, stats), recall, None
