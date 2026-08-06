"""artifact TTL — expires_at column on artifacts

Revision ID: 0003
Revises: 0002
Create Date: 2026-05-03

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "artifacts",
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("artifacts_expires_at_idx", "artifacts", ["expires_at"])
    op.create_index("gen_jobs_final_artifact_idx", "generation_jobs", ["final_artifact_id"])


def downgrade() -> None:
    op.drop_index("gen_jobs_final_artifact_idx", table_name="generation_jobs")
    op.drop_index("artifacts_expires_at_idx", table_name="artifacts")
    op.drop_column("artifacts", "expires_at")
