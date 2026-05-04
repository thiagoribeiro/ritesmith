"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-04-27

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, TSVECTOR

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "artifacts",
        sa.Column("artifact_id", sa.Text, primary_key=True),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("artifact_type", sa.Text, nullable=False),
        sa.Column("current_version", sa.Integer, nullable=False, server_default="1"),
        sa.Column("status", sa.Text, nullable=False, server_default="draft"),
        sa.Column("description", sa.Text),
        sa.Column("tags", ARRAY(sa.Text)),
        sa.Column("content_hash", sa.Text),
        sa.Column("generated_by_plan_id", sa.Text),
        sa.Column("search_vector", TSVECTOR),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.create_index(
        "artifacts_search_vector_gin",
        "artifacts",
        ["search_vector"],
        postgresql_using="gin",
    )
    op.create_index("artifacts_type_status_idx", "artifacts", ["artifact_type", "status"])

    op.create_table(
        "artifact_versions",
        sa.Column("artifact_id", sa.Text, sa.ForeignKey("artifacts.artifact_id", ondelete="CASCADE"), primary_key=True),
        sa.Column("version", sa.Integer, primary_key=True),
        sa.Column("content", sa.Text, nullable=False),
        sa.Column("input_schema", JSONB),
        sa.Column("output_schema", JSONB),
        sa.Column("risk_level", sa.Text),
        sa.Column("side_effects", sa.Text),
        sa.Column("deterministic", sa.Boolean, server_default="true"),
        sa.Column("requires_approval", sa.Boolean, server_default="false"),
        sa.Column("allowed_primitives", JSONB),
        sa.Column("metadata", JSONB),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )

    op.create_table(
        "capabilities",
        sa.Column("capability_id", sa.Text, primary_key=True),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column("status", sa.Text, nullable=False, server_default="available"),
        sa.Column("description", sa.Text),
        sa.Column("input_schema", JSONB),
        sa.Column("output_schema", JSONB),
        sa.Column("risk_level", sa.Text, nullable=False, server_default="low"),
        sa.Column("tags", ARRAY(sa.Text)),
        sa.Column("metadata", JSONB),
        sa.Column("provider_id", sa.Text),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )

    op.create_table(
        "plans",
        sa.Column("plan_id", sa.Text, primary_key=True),
        sa.Column("status", sa.Text, nullable=False, server_default="draft"),
        sa.Column("intent", sa.Text, nullable=False),
        sa.Column("summary", sa.Text),
        sa.Column("mode", sa.Text, nullable=False, server_default="propose"),
        sa.Column("reuse_policy", sa.Text, nullable=False, server_default="prefer_reuse"),
        sa.Column("constraints", JSONB),
        sa.Column("context", JSONB),
        sa.Column("steps", JSONB),
        sa.Column("artifact_ids", ARRAY(sa.Text)),
        sa.Column("policy_decision", JSONB),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("approved_at", sa.DateTime(timezone=True)),
    )

    op.create_table(
        "executions",
        sa.Column("execution_id", sa.Text, primary_key=True),
        sa.Column("artifact_id", sa.Text, nullable=False),
        sa.Column("artifact_version", sa.Integer),
        sa.Column("plan_id", sa.Text),
        sa.Column("status", sa.Text, nullable=False, server_default="queued"),
        sa.Column("runtime", sa.Text),
        sa.Column("input_json", JSONB),
        sa.Column("output_json", JSONB),
        sa.Column("error_json", JSONB),
        sa.Column("idempotency_key", sa.Text),
        sa.Column("delegated_execution_id", sa.Text),
        sa.Column("dry_run", sa.Boolean, server_default="false"),
        sa.Column("approval_token", sa.Text),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.create_index("executions_artifact_id_idx", "executions", ["artifact_id"])
    op.create_index("executions_idempotency_key_idx", "executions", ["idempotency_key"])
    op.create_index("executions_status_idx", "executions", ["status"])

    op.create_table(
        "generation_jobs",
        sa.Column("generation_id", sa.Text, primary_key=True),
        sa.Column("goal", sa.Text, nullable=False),
        sa.Column("target_type", sa.Text, nullable=False),
        sa.Column("status", sa.Text, nullable=False, server_default="pending"),
        sa.Column("input_schema", JSONB),
        sa.Column("output_schema", JSONB),
        sa.Column("constraints", JSONB),
        sa.Column("attempts", sa.Integer, nullable=False, server_default="0"),
        sa.Column("final_artifact_id", sa.Text),
        sa.Column("plan_id", sa.Text),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
    )

    op.create_table(
        "generation_attempts",
        sa.Column(
            "generation_id",
            sa.Text,
            sa.ForeignKey("generation_jobs.generation_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("attempt_number", sa.Integer, primary_key=True),
        sa.Column("content", sa.Text),
        sa.Column("validation_result", JSONB),
        sa.Column("error", sa.Text),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )

    op.create_table(
        "policy_decisions",
        sa.Column("decision_id", sa.Text, primary_key=True),
        sa.Column("operation", sa.Text, nullable=False),
        sa.Column("artifact_type", sa.Text),
        sa.Column("risk_level", sa.Text),
        sa.Column("decision", sa.Text, nullable=False),
        sa.Column("reason", sa.Text),
        sa.Column("context", JSONB),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )

    op.create_table(
        "idempotency_keys",
        sa.Column("key", sa.Text, primary_key=True),
        sa.Column("scope", sa.Text, nullable=False),
        sa.Column("execution_id", sa.Text),
        sa.Column("status", sa.Text, nullable=False, server_default="pending"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
    )

    op.create_table(
        "provider_manifests",
        sa.Column("provider_id", sa.Text, primary_key=True),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("url", sa.Text),
        sa.Column("manifest", JSONB),
        sa.Column("status", sa.Text, nullable=False, server_default="active"),
        sa.Column("last_discovered_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )

    op.create_table(
        "audit_events",
        sa.Column("event_id", sa.Text, primary_key=True),
        sa.Column("event_type", sa.Text, nullable=False),
        sa.Column("entity_type", sa.Text),
        sa.Column("entity_id", sa.Text),
        sa.Column("actor", sa.Text),
        sa.Column("payload", JSONB),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.create_index("audit_events_entity_idx", "audit_events", ["entity_type", "entity_id"])
    op.create_index("audit_events_event_type_idx", "audit_events", ["event_type"])
    op.create_index("audit_events_created_at_idx", "audit_events", ["created_at"])


def downgrade() -> None:
    op.drop_table("audit_events")
    op.drop_table("provider_manifests")
    op.drop_table("idempotency_keys")
    op.drop_table("policy_decisions")
    op.drop_table("generation_attempts")
    op.drop_table("generation_jobs")
    op.drop_table("executions")
    op.drop_table("plans")
    op.drop_table("capabilities")
    op.drop_table("artifact_versions")
    op.drop_table("artifacts")
