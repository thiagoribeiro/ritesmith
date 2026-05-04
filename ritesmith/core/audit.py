import logging

from sqlalchemy.ext.asyncio import AsyncSession

from ritesmith.core.ids import generate_id
from ritesmith.observability.metrics import audit_write_failures_total
from ritesmith.registry.models import AuditEvent

logger = logging.getLogger(__name__)


class AuditLogger:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def log_event(
        self,
        event_type: str,
        entity_type: str | None = None,
        entity_id: str | None = None,
        actor: str | None = None,
        payload: dict | None = None,
    ) -> None:
        try:
            event = AuditEvent(
                event_id=generate_id("evt"),
                event_type=event_type,
                entity_type=entity_type,
                entity_id=entity_id,
                actor=actor,
                payload=payload,
            )
            self.db.add(event)
            await self.db.flush()
        except Exception:
            logger.error(
                "audit write failed — event lost",
                exc_info=True,
                extra={"event_type": event_type, "entity_id": entity_id},
            )
            audit_write_failures_total.labels(event_type=event_type).inc()
