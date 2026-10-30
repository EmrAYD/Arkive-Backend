"""ai_results: kaynak dosya silinince kayıt kalsın (file_id boşalır) ve içerik özetiyle eşleşsin

- ai_results.content_hash: kaynak dosyanın içerik özeti; aynı dosya yeniden yüklenince özet hazır gelir.
- ai_results.file_id: boş olabilir, FK ON DELETE SET NULL (eskiden CASCADE ve zorunluydu).

Revision ID: 0018
Revises: 0017
Create Date: 2026-10-27
"""
from alembic import op
import sqlalchemy as sa

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


def _file_fk_name() -> str | None:
    for fk in sa.inspect(op.get_bind()).get_foreign_keys("ai_results"):
        if fk["constrained_columns"] == ["file_id"]:
            return fk["name"]
    return None


def upgrade() -> None:
    name = _file_fk_name()
    with op.batch_alter_table("ai_results") as batch:
        batch.add_column(sa.Column("content_hash", sa.String(length=64), nullable=True))
        batch.create_index("ix_ai_results_content_hash", ["content_hash"])
        if name:
            batch.drop_constraint(name, type_="foreignkey")
        batch.alter_column("file_id", existing_type=sa.Uuid(), nullable=True)
        batch.create_foreign_key("fk_ai_results_file", "files", ["file_id"], ["id"], ondelete="SET NULL")
    op.execute(
        "UPDATE ai_results SET content_hash = "
        "(SELECT files.content_hash FROM files WHERE files.id = ai_results.file_id)"
    )


def downgrade() -> None:
    # Kaynağı olmayan kayıtlar eski şemaya sığmaz (file_id zorunluydu): silinir.
    op.execute("DELETE FROM ai_results WHERE file_id IS NULL")
    with op.batch_alter_table("ai_results") as batch:
        batch.drop_constraint("fk_ai_results_file", type_="foreignkey")
        batch.alter_column("file_id", existing_type=sa.Uuid(), nullable=False)
        batch.create_foreign_key("fk_ai_results_file", "files", ["file_id"], ["id"], ondelete="CASCADE")
        batch.drop_index("ix_ai_results_content_hash")
        batch.drop_column("content_hash")
