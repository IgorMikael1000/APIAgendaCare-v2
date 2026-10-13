"""Add concurrent B-tree indexes for common AgendaCare filters.

Revision ID: 20261013_perfidx
Revises:
Create Date: 2026-10-13
"""
from alembic import op

revision = "20261013_perfidx"
down_revision = None
branch_labels = None
depends_on = None


INDEX_STATEMENTS = (
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS "
    "ix_patients_professional_id "
    "ON patients USING btree (professional_id)",
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS "
    "ix_appointments_professional_date_time_epoch "
    "ON appointments USING btree (professional_id, date_time_epoch)",
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS "
    "ix_appointments_patient_id "
    "ON appointments USING btree (patient_id)",
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS "
    "ix_evolution_records_professional_date_epoch "
    "ON evolution_records USING btree (professional_id, date_epoch)",
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS "
    "ix_evolution_records_patient_id "
    "ON evolution_records USING btree (patient_id)",
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS "
    "ix_evolution_records_appointment_id "
    "ON evolution_records USING btree (appointment_id)",
)


def upgrade() -> None:
    with op.get_context().autocommit_block():
        for statement in INDEX_STATEMENTS:
            op.execute(statement)


def downgrade() -> None:
    raise NotImplementedError(
        "This indexes-only revision intentionally has no destructive downgrade."
    )
