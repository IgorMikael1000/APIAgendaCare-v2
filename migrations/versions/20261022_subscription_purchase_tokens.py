"""Track current and linked Google Play subscription tokens.

Revision ID: 20261022_subscription_purchase_tokens
Revises: 20261013_updated_at_sync
Create Date: 2026-10-22
"""
from alembic import op
import sqlalchemy as sa


revision = "20261022_subscription_purchase_tokens"
down_revision = "20261013_updated_at_sync"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "subscription_purchase_tokens",
        sa.Column("purchase_token", sa.String(length=512), nullable=False),
        sa.Column("professional_id", sa.String(length=255), nullable=False),
        sa.ForeignKeyConstraint(
            ["professional_id"],
            ["professionals.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("purchase_token"),
    )
    op.create_index(
        "ix_subscription_purchase_tokens_professional_id",
        "subscription_purchase_tokens",
        ["professional_id"],
        unique=False,
    )
    op.execute(
        sa.text(
            "INSERT INTO subscription_purchase_tokens "
            "(purchase_token, professional_id) "
            "SELECT p.purchase_token, p.id "
            "FROM professionals AS p "
            "WHERE p.purchase_token IS NOT NULL "
            "AND NOT EXISTS ("
            "SELECT 1 FROM subscription_purchase_tokens AS t "
            "WHERE t.purchase_token = p.purchase_token"
            ")"
        )
    )


def downgrade() -> None:
    op.drop_index(
        "ix_subscription_purchase_tokens_professional_id",
        table_name="subscription_purchase_tokens",
    )
    op.drop_table("subscription_purchase_tokens")
