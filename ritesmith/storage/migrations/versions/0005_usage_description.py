"""artifact usage_description + reindex search_vector

Adds a canonical "use this when…" field, distinct from the prose description,
and folds it into the FTS search_vector (weight B) to improve reuse recall.

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-04

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# name (A) + usage_description (B) + description (B) + tags (C). usage_description
# is the "use this when…" trigger phrasing, weighted with the prose description.
_CREATE_TRIGGER_FN = """
CREATE OR REPLACE FUNCTION artifacts_search_vector_update()
RETURNS trigger AS $$
BEGIN
    NEW.search_vector :=
        setweight(to_tsvector('english', coalesce(NEW.name, '')), 'A') ||
        setweight(to_tsvector('english', coalesce(NEW.usage_description, '')), 'B') ||
        setweight(to_tsvector('english', coalesce(NEW.description, '')), 'B') ||
        setweight(to_tsvector('english', coalesce(array_to_string(NEW.tags, ' '), '')), 'C');
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""

# Trigger must also fire when usage_description changes.
_DROP_TRIGGER = "DROP TRIGGER IF EXISTS artifacts_search_vector_trigger ON artifacts;"
_CREATE_TRIGGER = """
CREATE TRIGGER artifacts_search_vector_trigger
BEFORE INSERT OR UPDATE OF name, usage_description, description, tags
ON artifacts
FOR EACH ROW
EXECUTE FUNCTION artifacts_search_vector_update();
"""

# Revert to the pre-0005 trigger (name + description + tags only).
_OLD_TRIGGER_FN = """
CREATE OR REPLACE FUNCTION artifacts_search_vector_update()
RETURNS trigger AS $$
BEGIN
    NEW.search_vector :=
        setweight(to_tsvector('english', coalesce(NEW.name, '')), 'A') ||
        setweight(to_tsvector('english', coalesce(NEW.description, '')), 'B') ||
        setweight(to_tsvector('english', coalesce(array_to_string(NEW.tags, ' '), '')), 'C');
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""
_OLD_CREATE_TRIGGER = """
CREATE TRIGGER artifacts_search_vector_trigger
BEFORE INSERT OR UPDATE OF name, description, tags
ON artifacts
FOR EACH ROW
EXECUTE FUNCTION artifacts_search_vector_update();
"""


def upgrade() -> None:
    op.add_column("artifacts", sa.Column("usage_description", sa.Text(), nullable=True))
    op.execute(_CREATE_TRIGGER_FN)
    op.execute(_DROP_TRIGGER)
    op.execute(_CREATE_TRIGGER)
    # Repopulate search_vector for existing rows (no usage_description yet, but this
    # re-runs the new trigger definition so weights stay consistent).
    op.execute("UPDATE artifacts SET usage_description = usage_description;")


def downgrade() -> None:
    op.execute(_DROP_TRIGGER)
    op.execute(_OLD_TRIGGER_FN)
    op.execute(_OLD_CREATE_TRIGGER)
    op.drop_column("artifacts", "usage_description")
