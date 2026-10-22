"""create ai_results

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-11
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ai_results",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "file_id",
            sa.Uuid(),
            sa.ForeignKey("files.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "folder_id",
            sa.Uuid(),
            sa.ForeignKey("folders.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("status", sa.String(length=16), server_default="pending", nullable=False),
        sa.Column(
            "result",
            sa.JSON().with_variant(postgresql.JSONB(), "postgresql"),
            nullable=True,
        ),
        sa.Column(
            "date",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'processing', 'done', 'failed')", name="ck_ai_results_status"
        ),
    )
    op.create_index("ix_ai_results_file_id", "ai_results", ["file_id"])
    op.create_index("ix_ai_results_folder_id", "ai_results", ["folder_id"])


def downgrade() -> None:
    op.drop_index("ix_ai_results_folder_id", table_name="ai_results")
    op.drop_index("ix_ai_results_file_id", table_name="ai_results")
    op.drop_table("ai_results")
