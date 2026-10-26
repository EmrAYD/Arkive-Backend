"""folders.emoji: klasör simgesinin ortasında gösterilen isteğe bağlı emoji

Revision ID: 0012
Revises: 0011
Create Date: 2026-10-26
"""
from alembic import op
import sqlalchemy as sa

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("folders", sa.Column("emoji", sa.String(length=64), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("folders") as batch:
        batch.drop_column("emoji")
