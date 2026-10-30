"""file_summaries tablosu ve files.content_hash: yapay zeka özetleri (aynı içerik tek özeti paylaşır)

Revision ID: 0013
Revises: 0012
Create Date: 2026-10-27
"""
from alembic import op
import sqlalchemy as sa

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("files", sa.Column("content_hash", sa.String(length=64), nullable=True))
    op.create_index("ix_files_content_hash", "files", ["content_hash"])
    op.create_table(
        "file_summaries",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("source", sa.String(length=8), nullable=False),
        sa.Column("model", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("user_id", "content_hash", name="uq_file_summaries_user_hash"),
        sa.CheckConstraint("source IN ('local', 'cloud')", name="ck_file_summaries_source"),
    )
    op.create_index("ix_file_summaries_user_id", "file_summaries", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_file_summaries_user_id", table_name="file_summaries")
    op.drop_table("file_summaries")
    op.drop_index("ix_files_content_hash", table_name="files")
    with op.batch_alter_table("files") as batch:
        batch.drop_column("content_hash")
