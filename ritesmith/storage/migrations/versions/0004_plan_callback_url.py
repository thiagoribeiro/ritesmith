"""plan callback_url — add callback_url column to plans

Revision ID: 0004
Revises: 0003
Create Date: 2026-05-13

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "plans",
        sa.Column("callback_url", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("plans", "callback_url")
