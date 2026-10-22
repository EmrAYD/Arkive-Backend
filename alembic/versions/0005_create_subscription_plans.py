"""create subscription_plans (Free ve Plus planları eklenir)

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-11
"""
import uuid

from alembic import op
import sqlalchemy as sa

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

MB = 1024**2
GB = 1024**3


def upgrade() -> None:
    plans = op.create_table(
        "subscription_plans",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("price", sa.Numeric(10, 2), nullable=False),
        sa.Column("storage_limit", sa.BigInteger(), nullable=False),
        sa.UniqueConstraint("name"),
        sa.CheckConstraint("price >= 0", name="ck_subscription_plans_price"),
        sa.CheckConstraint("storage_limit > 0", name="ck_subscription_plans_storage_limit"),
    )
    # Başlangıç planları. Dosya başına sınır (Free için 10 MB) veritabanında değil,
    # yükleme kodunda kontrol edilir.
    op.bulk_insert(
        plans,
        [
            {"id": uuid.uuid4(), "name": "Free", "price": 0, "storage_limit": 100 * MB},
            {"id": uuid.uuid4(), "name": "Plus", "price": 49.99, "storage_limit": 100 * GB},
        ],
    )


def downgrade() -> None:
    op.drop_table("subscription_plans")
