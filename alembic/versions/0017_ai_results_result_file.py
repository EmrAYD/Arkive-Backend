"""ai_results.result_file_id: sonucun dosyası; dosya kalıcı silinince satır da silinir (yeniden özetlenebilir)

Revision ID: 0017
Revises: 0016
Create Date: 2026-10-27
"""
import json

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None

_JSON = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
_ai = sa.table(
    "ai_results",
    sa.column("id", sa.Uuid()),
    sa.column("result_file_id", sa.Uuid()),
    sa.column("result", _JSON),
)
_files = sa.table("files", sa.column("id", sa.Uuid()))


def upgrade() -> None:
    with op.batch_alter_table("ai_results") as batch:
        batch.add_column(sa.Column("result_file_id", sa.Uuid(), nullable=True))
        batch.create_foreign_key(
            "fk_ai_results_result_file", "files", ["result_file_id"], ["id"], ondelete="CASCADE"
        )
        batch.create_index("ix_ai_results_result_file_id", ["result_file_id"])

    # Eski kayıtlarda sonuç dosyasının kimliği result JSON'unun içindeydi: sütuna taşınır, JSON'dan çıkarılır.
    bind = op.get_bind()
    for row_id, result in bind.execute(sa.select(_ai.c.id, _ai.c.result)).all():
        if isinstance(result, str):
            result = json.loads(result)
        if not result or "result_file_id" not in result:
            continue
        raw = result.pop("result_file_id")
        file_id = None
        if raw:
            import uuid

            candidate = uuid.UUID(raw)
            if bind.execute(sa.select(_files.c.id).where(_files.c.id == candidate)).scalar() is not None:
                file_id = candidate
        bind.execute(sa.update(_ai).where(_ai.c.id == row_id).values(result_file_id=file_id, result=result))


def downgrade() -> None:
    bind = op.get_bind()
    for row_id, file_id, result in bind.execute(
        sa.select(_ai.c.id, _ai.c.result_file_id, _ai.c.result)
    ).all():
        if isinstance(result, str):
            result = json.loads(result)
        if result:
            result["result_file_id"] = str(file_id) if file_id else None
            bind.execute(sa.update(_ai).where(_ai.c.id == row_id).values(result=result))
    with op.batch_alter_table("ai_results") as batch:
        batch.drop_index("ix_ai_results_result_file_id")
        batch.drop_constraint("fk_ai_results_result_file", type_="foreignkey")
        batch.drop_column("result_file_id")
