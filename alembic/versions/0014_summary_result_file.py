"""file_summaries.result_file_id: özetin AI Sonuçlarım klasöründeki .txt dosyası

Revision ID: 0014
Revises: 0013
Create Date: 2026-10-27
"""
from alembic import op
import sqlalchemy as sa

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("file_summaries") as batch:
        batch.add_column(sa.Column("result_file_id", sa.Uuid(), nullable=True))
        batch.create_foreign_key(
            "fk_file_summaries_result_file", "files", ["result_file_id"], ["id"], ondelete="SET NULL"
        )
        batch.create_index("ix_file_summaries_result_file_id", ["result_file_id"])


def downgrade() -> None:
    with op.batch_alter_table("file_summaries") as batch:
        batch.drop_index("ix_file_summaries_result_file_id")
        batch.drop_constraint("fk_file_summaries_result_file", type_="foreignkey")
        batch.drop_column("result_file_id")
