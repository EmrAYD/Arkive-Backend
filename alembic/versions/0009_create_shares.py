"""create shares

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-11
"""
from alembic import op
import sqlalchemy as sa

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "shares",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "file_id",
            sa.Uuid(),
            sa.ForeignKey("files.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "shared_by",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "shared_with",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("permission", sa.String(length=8), server_default="view", nullable=False),
        sa.CheckConstraint("permission IN ('view', 'edit')", name="ck_shares_permission"),
        sa.CheckConstraint("shared_by <> shared_with", name="ck_shares_not_self"),
        sa.UniqueConstraint("file_id", "shared_with", name="uq_shares_file_shared_with"),
    )
    op.create_index("ix_shares_file_id", "shares", ["file_id"])
    op.create_index("ix_shares_shared_by", "shares", ["shared_by"])
    op.create_index("ix_shares_shared_with", "shares", ["shared_with"])


def downgrade() -> None:
    op.drop_index("ix_shares_shared_with", table_name="shares")
    op.drop_index("ix_shares_shared_by", table_name="shares")
    op.drop_index("ix_shares_file_id", table_name="shares")
    op.drop_table("shares")
