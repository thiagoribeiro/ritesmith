"""Startup hook: upsert provider manifests and capabilities into the database."""

from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncSession

from ritesmith.runtime.providers import PROVIDERS

logger = logging.getLogger(__name__)


async def register_all_providers(db: AsyncSession) -> None:
    """Called from the FastAPI lifespan on startup."""
    from datetime import UTC, datetime

    from sqlalchemy.dialects.postgresql import insert as pg_insert

    from ritesmith.registry.models import Capability, ProviderManifest

    now = datetime.now(UTC)

    for provider in PROVIDERS:
        try:
            manifest = provider.manifest()
            stmt = (
                pg_insert(ProviderManifest)
                .values(
                    provider_id=provider.namespace,
                    name=provider.namespace,
                    manifest=manifest,
                    status="active" if provider.is_available() else "unavailable",
                    last_discovered_at=now,
                )
                .on_conflict_do_update(
                    index_elements=["provider_id"],
                    set_={
                        "manifest": manifest,
                        "status": "active" if provider.is_available() else "unavailable",
                        "last_discovered_at": now,
                    },
                )
            )
            await db.execute(stmt)

            if provider.is_available():
                functions = provider.lua_functions()
                for cap in provider.capabilities():
                    cap_id = (
                        f"{provider.namespace}.{cap['name']}"
                        if "." not in cap["name"]
                        else cap["name"]
                    )
                    # The registry is read by /capabilities, MCP and workflow generation;
                    # it must carry the same contracts the bridge enforces.
                    fn_def = functions.get(cap_id)
                    contract = {
                        "description": (fn_def and fn_def.description)
                        or cap.get("description", ""),
                        "input_schema": (fn_def and fn_def.input_schema) or {},
                        "output_schema": (fn_def and fn_def.output_schema) or {},
                    }
                    cap_stmt = (
                        pg_insert(Capability)
                        .values(
                            capability_id=cap_id,
                            name=cap_id,
                            kind="script_function",
                            status="available",
                            **contract,
                            risk_level=cap.get("risk_level", "low"),
                            tags=cap.get("tags", [provider.namespace]),
                            provider_id=provider.namespace,
                            metadata_={},
                        )
                        .on_conflict_do_update(
                            index_elements=["capability_id"],
                            set_={"status": "available", **contract},
                        )
                    )
                    await db.execute(cap_stmt)

            logger.info(
                "Provider registered: %s (available=%s)",
                provider.namespace,
                provider.is_available(),
            )
        except Exception as e:
            logger.warning("Failed to register provider %s: %s", provider.namespace, e)

    await db.commit()
