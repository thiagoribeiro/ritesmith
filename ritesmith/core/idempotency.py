from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from ritesmith.core.exceptions import ConflictError
from ritesmith.registry.models import IdempotencyKey


class IdempotencyService:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def get(self, key: str, scope: str) -> IdempotencyKey | None:
        stmt = select(IdempotencyKey).where(
            IdempotencyKey.key == key,
            IdempotencyKey.scope == scope,
        )
        result = await self.db.execute(stmt)
        return result.scalar_one_or_none()

    async def check_or_create(self, key: str, scope: str) -> tuple[bool, str | None]:
        """Returns (is_duplicate, existing_execution_id).

        Raises ConflictError if the key exists and is still pending (in-flight).
        Uses INSERT … ON CONFLICT DO NOTHING to eliminate the SELECT→INSERT TOCTOU race.
        """
        stmt = (
            pg_insert(IdempotencyKey)
            .values(key=key, scope=scope, status="pending")
            .on_conflict_do_nothing(index_elements=["key"])
            .returning(IdempotencyKey.key)
        )
        result = await self.db.execute(stmt)
        await self.db.flush()

        newly_inserted = result.scalar_one_or_none() is not None
        if newly_inserted:
            return False, None

        # A pre-existing record won the race — check its state
        existing = await self.get(key, scope)
        if existing is None:
            return False, None

        if existing.status == "pending":
            raise ConflictError(
                f"Request with idempotency key '{key}' is still in progress",
                details={"key": key, "scope": scope},
            )
        return True, existing.execution_id

    async def mark_complete(self, key: str, scope: str, execution_id: str) -> None:
        existing = await self.get(key, scope)
        if existing:
            existing.status = "complete"
            existing.execution_id = execution_id
            await self.db.flush()
