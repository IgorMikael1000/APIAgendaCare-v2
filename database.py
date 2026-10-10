import os
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from sqlalchemy.schema import CreateColumn

# A URL do PostgreSQL deve ser configurada na variável de ambiente DATABASE_URL.
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://usuario:senha@host/dbname?sslmode=require")

engine = create_engine(DATABASE_URL, pool_pre_ping=True, pool_recycle=300)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

def create_or_update_schema():
    Base.metadata.create_all(bind=engine)

    with engine.begin() as connection:
        inspector = inspect(connection)
        preparer = connection.dialect.identifier_preparer

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