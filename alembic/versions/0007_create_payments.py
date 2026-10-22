"""create payments

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-11
"""
from alembic import op
import sqlalchemy as sa

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "payments",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "user_subscription_id",
            sa.Uuid(),
            sa.ForeignKey("user_subscriptions.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("provider_payment_id", sa.String(length=255), nullable=False),
        sa.Column("price", sa.Numeric(10, 2), nullable=False),
        sa.Column(
            "date",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("status", sa.String(length=16), server_default="pending", nullable=False),
        sa.UniqueConstraint("provider_payment_id"),
        sa.CheckConstraint("price >= 0", name="ck_payments_price"),
        sa.CheckConstraint(
            "status IN ('pending', 'paid', 'failed', 'refunded')", name="ck_payments_status"
        ),
    )
    op.create_index("ix_payments_user_subscription_id", "payments", ["user_subscription_id"])


def downgrade() -> None:
    op.drop_index("ix_payments_user_subscription_id", table_name="payments")
    op.drop_table("payments")
