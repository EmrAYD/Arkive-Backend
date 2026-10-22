"""folders: color ve favorite alanları

ER diyagramındaki Folders.Color ve Folders.Favorite alanları.

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-11
"""
from alembic import op
import sqlalchemy as sa

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("folders", sa.Column("color", sa.String(length=20), nullable=True))
    op.add_column(
        "folders",
        sa.Column("favorite", sa.Boolean(), server_default=sa.false(), nullable=False),
    )


def downgrade() -> None:
    with op.batch_alter_table("folders") as batch:
        batch.drop_column("favorite")
        batch.drop_column("color")
