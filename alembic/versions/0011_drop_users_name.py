"""users.name sütununu kaldırır (kullanıcının görünen adı artık tutulmuyor)

Revision ID: 0011
Revises: 0010
Create Date: 2026-10-23
"""
from alembic import op
import sqlalchemy as sa

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # batch_alter_table: SQLite'ta (geliştirme veritabanı) sütun silmek tabloyu yeniden kurmayı gerektirir;
    # PostgreSQL'de doğrudan ALTER TABLE ... DROP COLUMN olarak çalışır.
    with op.batch_alter_table("users") as batch:
        batch.drop_column("name")


def downgrade() -> None:
    # Silinen veriler geri gelmez; yalnızca boş (NULL) sütun yeniden eklenir.
    with op.batch_alter_table("users") as batch:
        batch.add_column(sa.Column("name", sa.String(length=255), nullable=True))
