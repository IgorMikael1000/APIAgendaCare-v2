"""Persist Google Play RTDN events until their account can be verified.

Revision ID: 20261023_google_play_webhook_events
Revises: 20261022_subscription_purchase_tokens
Create Date: 2026-10-23
"""
from alembic import op
import sqlalchemy as sa


revision = "20261023_google_play_webhook_events"
down_revision = "20261022_subscription_purchase_tokens"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "google_play_webhook_events",
        sa.Column("event_key", sa.String(length=64), nullable=False),
        sa.Column("purchase_token_hash", sa.String(length=64), nullable=True),
        sa.Column("notification_type", sa.Integer(), nullable=True),
        sa.Column("event_time_millis", sa.String(length=32), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column(
            "received_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "attempt_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.PrimaryKeyConstraint("event_key"),
    )
    op.create_index(
        "ix_google_play_webhook_events_purchase_token_hash",
        "google_play_webhook_events",
        ["purchase_token_hash"],
        unique=False,
    )
    op.create_index(
        "ix_google_play_webhook_events_status",
        "google_play_webhook_events",
        ["status"],
        unique=False,
    )
    op.create_index(
        "ix_google_play_webhook_events_received_at",
        "google_play_webhook_events",
        ["received_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_google_play_webhook_events_received_at",
        table_name="google_play_webhook_events",
    )
    op.drop_index(
        "ix_google_play_webhook_events_status",
        table_name="google_play_webhook_events",
    )
    op.drop_index(
        "ix_google_play_webhook_events_purchase_token_hash",
        table_name="google_play_webhook_events",
    )
    op.drop_table("google_play_webhook_events")
