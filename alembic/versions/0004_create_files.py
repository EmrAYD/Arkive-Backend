"""create files

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-11
"""
from alembic import op
import sqlalchemy as sa

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "files",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "user_id",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "folder_id",
            sa.Uuid(),
            sa.ForeignKey("folders.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("size", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="uploading", nullable=False),
        sa.Column("storage_key", sa.String(length=512), nullable=False),
        sa.Column("mime_type", sa.String(length=255), nullable=True),
        sa.Column("favorite", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.UniqueConstraint("storage_key"),
        sa.CheckConstraint("size >= 0", name="ck_files_size"),
        sa.CheckConstraint(
            "status IN ('uploading', 'ready', 'failed', 'deleted')", name="ck_files_status"
        ),
    )
    op.create_index("ix_files_user_id", "files", ["user_id"])
    op.create_index("ix_files_folder_id", "files", ["folder_id"])


def downgrade() -> None:
    op.drop_index("ix_files_folder_id", table_name="files")
    op.drop_index("ix_files_user_id", table_name="files")
    op.drop_table("files")
