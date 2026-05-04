"""fts trigger and search_vector population

Revision ID: 0002
Revises: 0001
Create Date: 2026-04-27

"""
from typing import Sequence, Union

from alembic import op

revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Trigger function: concatena name + description + tags e atualiza search_vector
_CREATE_TRIGGER_FN = """
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

_CREATE_TRIGGER = """
CREATE TRIGGER artifacts_search_vector_trigger
BEFORE INSERT OR UPDATE OF name, description, tags
ON artifacts
FOR EACH ROW
EXECUTE FUNCTION artifacts_search_vector_update();
"""

_DROP_TRIGGER = "DROP TRIGGER IF EXISTS artifacts_search_vector_trigger ON artifacts;"
_DROP_TRIGGER_FN = "DROP FUNCTION IF EXISTS artifacts_search_vector_update();"


def upgrade() -> None:
    op.execute(_CREATE_TRIGGER_FN)
    op.execute(_CREATE_TRIGGER)


def downgrade() -> None:
    op.execute(_DROP_TRIGGER)
    op.execute(_DROP_TRIGGER_FN)
