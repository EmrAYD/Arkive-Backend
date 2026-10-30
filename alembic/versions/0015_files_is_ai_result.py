"""files.is_ai_result: yapay zeka sonuç dosyaları AI Sonuçlarım sayfasının en üstünde de durabilir

Revision ID: 0015
Revises: 0014
Create Date: 2026-10-27
"""
from alembic import op
import sqlalchemy as sa

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("files", sa.Column("is_ai_result", sa.Boolean(), nullable=False, server_default=sa.false()))
    # Daha önce AI Sonuçlarım klasörlerine yazılmış dosyalar sonuç dosyası sayılır.
    op.execute(
        "UPDATE files SET is_ai_result = TRUE "
        "WHERE folder_id IN (SELECT id FROM folders WHERE location = 'ai_results')"
    )


def downgrade() -> None:
    with op.batch_alter_table("files") as batch:
        batch.drop_column("is_ai_result")
