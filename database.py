import logging
import os
from datetime import datetime, timedelta, timezone
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool
from sqlalchemy.schema import CreateColumn

# A URL do PostgreSQL deve ser configurada na variável de ambiente DATABASE_URL.
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://usuario:senha@host/dbname?sslmode=require")

engine = create_engine(DATABASE_URL, poolclass=NullPool)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()
logger = logging.getLogger(__name__)

PERFORMANCE_INDEX_STATEMENTS = (
    (
        "ix_patients_professional_id",
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS "
        "ix_patients_professional_id "
        "ON patients USING btree (professional_id)",
    ),
    (
        "ix_appointments_professional_date_time_epoch",
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS "
        "ix_appointments_professional_date_time_epoch "
        "ON appointments USING btree (professional_id, date_time_epoch)",
    ),
    (
        "ix_appointments_patient_id",
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS "
        "ix_appointments_patient_id "
        "ON appointments USING btree (patient_id)",
    ),
    (
        "ix_evolution_records_professional_date_epoch",
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS "
        "ix_evolution_records_professional_date_epoch "
        "ON evolution_records USING btree (professional_id, date_epoch)",
    ),
    (
        "ix_evolution_records_patient_id",
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS "
        "ix_evolution_records_patient_id "
        "ON evolution_records USING btree (patient_id)",
    ),
    (
        "ix_evolution_records_appointment_id",
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS "
        "ix_evolution_records_appointment_id "
        "ON evolution_records USING btree (appointment_id)",
    ),
)


def criar_indices_performance():
    if engine.dialect.name != "postgresql":
        logger.warning(
            "Índices de performance não criados: PostgreSQL é necessário "
            "(dialeto atual: %s)",
            engine.dialect.name,
        )
        return

    try:
        with engine.connect().execution_options(
            isolation_level="AUTOCOMMIT"
        ) as connection:
            for index_name, statement in PERFORMANCE_INDEX_STATEMENTS:
                try:
                    connection.execute(text(statement))
                except Exception:
                    logger.exception(
                        "Falha ao criar o índice de performance %s",
                        index_name,
                    )
                else:
                    logger.info(
                        "Índice de performance verificado: %s",
                        index_name,
                    )
    except Exception:
        logger.exception(
            "Não foi possível conectar ao banco para verificar índices; "
            "a API continuará iniciando"
        )


def _migrate_legacy_trial_devices(connection, inspector, preparer):
    professionals_table = "professionals"
    trial_devices_table = "dispositivos_trial"
    if not inspector.has_table(professionals_table):
        return

    columns = {
        column["name"] for column in inspector.get_columns(professionals_table)
    }
    if "device_id" in columns:
        selected_columns = ["device_id"]
        if "trial_ends_at" in columns:
            selected_columns.append("trial_ends_at")
        results = connection.execute(
            text(
                "SELECT "
                + ", ".join(preparer.quote(column) for column in selected_columns)
                + f" FROM {preparer.quote(professionals_table)} "
                "WHERE device_id IS NOT NULL"
            )
        )
        for row in results:
            android_id = (row[0] or "").strip()
            if len(android_id) < 6:
                continue

            trial_ends_at = row[1] if len(row) > 1 else None
            activation_date = (
                trial_ends_at - timedelta(days=7)
                if isinstance(trial_ends_at, datetime)
                else datetime.now(timezone.utc)
            )
            if activation_date.tzinfo is None:
                activation_date = activation_date.replace(tzinfo=timezone.utc)
            connection.execute(
                text(
                    f"INSERT INTO {preparer.quote(trial_devices_table)} "
                    "(android_id, data_ativacao) "
                    "VALUES (:android_id, :data_ativacao) "
                    "ON CONFLICT (android_id) DO NOTHING"
                ),
                {
                    "android_id": android_id,
                    "data_ativacao": activation_date,
                },
            )

    drop_if_exists = (
        "DROP COLUMN IF EXISTS"
        if connection.dialect.name == "postgresql"
        else "DROP COLUMN"
    )
    for column_name in (
        "device_id",
        "password_hash",
        "reset_token",
        "reset_tokens",
        "reset_token_expires_at",
        "reset_password_token",
        "reset_password_expires_at",
    ):
        if column_name in columns:
            if column_name == "device_id" and connection.dialect.name == "sqlite":
                # The legacy SQLite column has an inline unique constraint.
                continue
            connection.execute(
                text(
                    f"ALTER TABLE {preparer.quote(professionals_table)} "
                    f"{drop_if_exists} {preparer.quote(column_name)}"
                )
            )

    for table_name in ("reset_tokens", "password_reset_tokens"):
        if inspector.has_table(table_name):
            connection.execute(
                text(
                    f"DROP TABLE IF EXISTS {preparer.quote(table_name)}"
                )
            )


def create_or_update_schema():
    Base.metadata.create_all(bind=engine)

    with engine.begin() as connection:
        inspector = inspect(connection)
        preparer = connection.dialect.identifier_preparer

        if inspector.has_table("app_settings"):
            connection.execute(
                text(
                    "INSERT INTO app_settings "
                    "(id, latest_version_code, force_update) "
                    "SELECT 1, 21, TRUE "
                    "WHERE NOT EXISTS ("
                    "SELECT 1 FROM app_settings WHERE id = 1"
                    ")"
                )
            )

        for table in Base.metadata.sorted_tables:
            if not inspector.has_table(table.name):
                continue

            existing_columns = {
                column["name"] for column in inspector.get_columns(table.name)
            }
            quoted_table = preparer.quote(table.name)
            for column in table.columns:
                if column.name in existing_columns:
                    continue

                column_ddl = CreateColumn(column).compile(dialect=connection.dialect)
                connection.execute(
                    text(f"ALTER TABLE {quoted_table} ADD COLUMN {column_ddl}")
                )
                existing_columns.add(column.name)

        _migrate_legacy_trial_devices(connection, inspector, preparer)

        for table in Base.metadata.sorted_tables:
            if inspector.has_table(table.name):
                for index in table.indexes:
                    index.create(bind=connection, checkfirst=True)

        for table in Base.metadata.sorted_tables:
            if not inspector.has_table(table.name):
                continue

            unique_columns = {
                tuple(constraint.get("column_names", []))
                for constraint in inspector.get_unique_constraints(table.name)
            }
            unique_indexes = {
                tuple(index.get("column_names", []))
                for index in inspector.get_indexes(table.name)
                if index.get("unique")
            }
            for column in table.columns:
                if not column.unique:
                    continue
                column_key = (column.name,)
                if column_key in unique_columns or column_key in unique_indexes:
                    continue

                constraint_name = f"uq_{table.name}_{column.name}"
                if connection.dialect.name == "sqlite":
                    connection.execute(
                        text(
                            f"CREATE UNIQUE INDEX "
                            f"{preparer.quote(constraint_name)} "
                            f"ON {preparer.quote(table.name)} "
                            f"({preparer.quote(column.name)})"
                        )
                    )
                else:
                    connection.execute(
                        text(
                            f"ALTER TABLE {preparer.quote(table.name)} "
                            f"ADD CONSTRAINT {preparer.quote(constraint_name)} "
                            f"UNIQUE ({preparer.quote(column.name)})"
                        )
                    )

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()