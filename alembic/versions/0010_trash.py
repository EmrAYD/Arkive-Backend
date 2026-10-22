"""çöp kutusu: files/folders.deleted_at ve users.trash_retention_days

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-12
"""
from alembic import op
import sqlalchemy as sa

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("files", sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_files_deleted_at", "files", ["deleted_at"])

    op.add_column("folders", sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_folders_deleted_at", "folders", ["deleted_at"])

    # Aynı ad benzersizliği yalnızca çöpte OLMAYAN klasörler için geçerli: çöpteki "Belgeler" klasörü
    # yeni bir "Belgeler" oluşturmayı engellememeli.
    op.drop_index("uq_folders_child_name", table_name="folders")
    op.drop_index("uq_folders_root_name", table_name="folders")
    op.create_index(
        "uq_folders_root_name",
        "folders",
        ["user_id", "location", "name"],
        unique=True,
        postgresql_where=sa.text("parent_id IS NULL AND deleted_at IS NULL"),
        sqlite_where=sa.text("parent_id IS NULL AND deleted_at IS NULL"),
    )
    op.create_index(
        "uq_folders_child_name",
        "folders",
        ["parent_id", "name"],
        unique=True,
        postgresql_where=sa.text("parent_id IS NOT NULL AND deleted_at IS NULL"),
        sqlite_where=sa.text("parent_id IS NOT NULL AND deleted_at IS NULL"),
    )

    op.add_column(
        "users",
        sa.Column("trash_retention_days", sa.Integer(), server_default="30", nullable=False),
    )
    op.create_check_constraint(
        "ck_users_trash_retention_days", "users", "trash_retention_days BETWEEN 1 AND 365"
    )


def downgrade() -> None:
    op.drop_constraint("ck_users_trash_retention_days", "users", type_="check")
    op.drop_column("users", "trash_retention_days")

    op.drop_index("uq_folders_child_name", table_name="folders")
    op.drop_index("uq_folders_root_name", table_name="folders")
    op.create_index(
        "uq_folders_root_name",
        "folders",
        ["user_id", "location", "name"],
        unique=True,
        postgresql_where=sa.text("parent_id IS NULL"),
        sqlite_where=sa.text("parent_id IS NULL"),
    )
    op.create_index(
        "uq_folders_child_name",
        "folders",
        ["parent_id", "name"],
        unique=True,
        postgresql_where=sa.text("parent_id IS NOT NULL"),
        sqlite_where=sa.text("parent_id IS NOT NULL"),
    )

    op.drop_index("ix_folders_deleted_at", table_name="folders")
    op.drop_column("folders", "deleted_at")
    op.drop_index("ix_files_deleted_at", table_name="files")
    op.drop_column("files", "deleted_at")
