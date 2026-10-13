"""Add updated_at fields for incremental synchronization.

Revision ID: 20261013_updated_at_sync
Revises: 20261013_perfidx
Create Date: 2026-10-13
"""
from alembic import op
import sqlalchemy as sa

revision = "20261013_updated_at_sync"
down_revision = "20261013_perfidx"
branch_labels = None
depends_on = None


INDEX_STATEMENTS = (
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS "
    "ix_patients_updated_at ON patients USING btree (updated_at)",
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS "
    "ix_appointments_updated_at ON appointments USING btree (updated_at)",
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS "
    "ix_evolution_records_updated_at "
    "ON evolution_records USING btree (updated_at)",
)

DROP_INDEX_STATEMENTS = (
    "DROP INDEX CONCURRENTLY IF EXISTS ix_patients_updated_at",
    "DROP INDEX CONCURRENTLY IF EXISTS ix_appointments_updated_at",
    "DROP INDEX CONCURRENTLY IF EXISTS ix_evolution_records_updated_at",
)

CREATE_UPDATED_AT_TRIGGER_FUNCTION = """
CREATE OR REPLACE FUNCTION set_updated_at_timestamp()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    NEW.updated_at = CURRENT_TIMESTAMP;
    RETURN NEW;
END;
$$
"""

CREATE_UPDATED_AT_TRIGGERS = (
    "CREATE TRIGGER trg_patients_updated_at "
    "BEFORE UPDATE ON patients "
    "FOR EACH ROW EXECUTE FUNCTION set_updated_at_timestamp()",
    "CREATE TRIGGER trg_appointments_updated_at "
    "BEFORE UPDATE ON appointments "
    "FOR EACH ROW EXECUTE FUNCTION set_updated_at_timestamp()",
    "CREATE TRIGGER trg_evolution_records_updated_at "
    "BEFORE UPDATE ON evolution_records "
    "FOR EACH ROW EXECUTE FUNCTION set_updated_at_timestamp()",
)


def upgrade() -> None:
    for table_name in ("patients", "appointments", "evolution_records"):
        op.add_column(
            table_name,
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("CURRENT_TIMESTAMP"),
            ),
        )

    op.execute(CREATE_UPDATED_AT_TRIGGER_FUNCTION)
    for statement in CREATE_UPDATED_AT_TRIGGERS:
        op.execute(statement)

    with op.get_context().autocommit_block():
        for statement in INDEX_STATEMENTS:
            op.execute(statement)


def downgrade() -> None:
    with op.get_context().autocommit_block():
        for statement in DROP_INDEX_STATEMENTS:
            op.execute(statement)

    op.execute("DROP TRIGGER IF EXISTS trg_patients_updated_at ON patients")
    op.execute("DROP TRIGGER IF EXISTS trg_appointments_updated_at ON appointments")
    op.execute(
        "DROP TRIGGER IF EXISTS trg_evolution_records_updated_at "
        "ON evolution_records"
    )
    op.execute("DROP FUNCTION IF EXISTS set_updated_at_timestamp()")

    for table_name in ("evolution_records", "appointments", "patients"):
        op.drop_column(table_name, "updated_at")
