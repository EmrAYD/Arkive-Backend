"""file_summaries tablosu kaldırıldı: özetler yalnızca ai_results tablosunda saklanır

Mevcut özetler ai_results satırlarına taşınır (kaynak dosya = aynı kullanıcının aynı içerikli bir dosyası; böyle bir dosya
kalmamış özetler zaten sahipsizdi, atılır). Özet metni, kaynak (local/cloud) ve model `result` JSON'una yazılır.

Revision ID: 0016
Revises: 0015
Create Date: 2026-10-27
"""
import json
import uuid

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None

_JSON = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")

_files = sa.table(
    "files",
    sa.column("id", sa.Uuid()),
    sa.column("user_id", sa.Uuid()),
    sa.column("folder_id", sa.Uuid()),
    sa.column("content_hash", sa.String()),
    sa.column("deleted_at", sa.DateTime(timezone=True)),
    sa.column("created_at", sa.DateTime(timezone=True)),
)
_summaries = sa.table(
    "file_summaries",
    sa.column("id", sa.Uuid()),
    sa.column("user_id", sa.Uuid()),
    sa.column("content_hash", sa.String()),
    sa.column("summary", sa.Text()),
    sa.column("source", sa.String()),
    sa.column("model", sa.String()),
    sa.column("created_at", sa.DateTime(timezone=True)),
    sa.column("result_file_id", sa.Uuid()),
)
_ai = sa.table(
    "ai_results",
    sa.column("id", sa.Uuid()),
    sa.column("file_id", sa.Uuid()),
    sa.column("folder_id", sa.Uuid()),
    sa.column("status", sa.String()),
    sa.column("result", _JSON),
    sa.column("date", sa.DateTime(timezone=True)),
)


def upgrade() -> None:
    bind = op.get_bind()
    rows = bind.execute(sa.select(_summaries)).mappings().all()
    for r in rows:
        source_id = bind.execute(
            sa.select(_files.c.id)
            .where(_files.c.user_id == r["user_id"], _files.c.content_hash == r["content_hash"])
            .order_by(_files.c.deleted_at.is_not(None), _files.c.created_at)
            .limit(1)
        ).scalar()
        if source_id is None:
            continue
        folder_id = None
        if r["result_file_id"] is not None:
            folder_id = bind.execute(
                sa.select(_files.c.folder_id).where(_files.c.id == r["result_file_id"])
            ).scalar()
        bind.execute(
            sa.insert(_ai).values(
                id=uuid.uuid4(),
                file_id=source_id,
                folder_id=folder_id,
                status="done",
                result={
                    "kind": "summary",
                    "summary": r["summary"],
                    "source": r["source"],
                    "model": r["model"],
                    "truncated": False,
                    "result_file_id": str(r["result_file_id"]) if r["result_file_id"] else None,
                },
                date=r["created_at"],
            )
        )
    op.drop_index("ix_file_summaries_result_file_id", table_name="file_summaries")
    op.drop_index("ix_file_summaries_user_id", table_name="file_summaries")
    op.drop_table("file_summaries")


def downgrade() -> None:
    op.create_table(
        "file_summaries",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("source", sa.String(length=8), nullable=False),
        sa.Column("model", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("result_file_id", sa.Uuid(), sa.ForeignKey("files.id", ondelete="SET NULL"), nullable=True),
        sa.UniqueConstraint("user_id", "content_hash", name="uq_file_summaries_user_hash"),
        sa.CheckConstraint("source IN ('local', 'cloud')", name="ck_file_summaries_source"),
    )
    op.create_index("ix_file_summaries_user_id", "file_summaries", ["user_id"])
    op.create_index("ix_file_summaries_result_file_id", "file_summaries", ["result_file_id"])

    bind = op.get_bind()
    seen: set[tuple] = set()
    rows = bind.execute(
        sa.select(_ai.c.result, _ai.c.date, _files.c.user_id, _files.c.content_hash)
        .join(_files, _ai.c.file_id == _files.c.id)
        .where(_ai.c.status == "done", _files.c.content_hash.is_not(None))
        .order_by(_ai.c.date)
    ).all()
    for result, date, user_id, content_hash in rows:
        if isinstance(result, str):
            result = json.loads(result)
        if not result or result.get("kind") != "summary" or (user_id, content_hash) in seen:
            continue
        seen.add((user_id, content_hash))
        rf = result.get("result_file_id")
        exists = rf and bind.execute(sa.select(_files.c.id).where(_files.c.id == uuid.UUID(rf))).scalar()
        bind.execute(
            sa.insert(_summaries).values(
                id=uuid.uuid4(),
                user_id=user_id,
                content_hash=content_hash,
                summary=result.get("summary", ""),
                source=result.get("source", "cloud"),
                model=result.get("model"),
                created_at=date,
                result_file_id=uuid.UUID(rf) if exists else None,
            )
        )
