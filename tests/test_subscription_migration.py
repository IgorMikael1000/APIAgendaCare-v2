import importlib.util
from pathlib import Path
import unittest

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text


MIGRATION_PATH = (
    Path(__file__).parents[1]
    / "migrations"
    / "versions"
    / "20261022_subscription_purchase_tokens.py"
)


class SubscriptionTokenMigrationTests(unittest.TestCase):
    def test_migration_backfills_existing_tokens_without_changing_profiles(self):
        spec = importlib.util.spec_from_file_location(
            "subscription_purchase_tokens_migration",
            MIGRATION_PATH,
        )
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)

        engine = create_engine("sqlite://")
        with engine.begin() as connection:
            connection.execute(
                text(
                    "CREATE TABLE professionals ("
                    "id VARCHAR(255) PRIMARY KEY, "
                    "purchase_token VARCHAR(512))"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO professionals (id, purchase_token) "
                    "VALUES ('professional-1', 'existing-token'), "
                    "('professional-2', NULL)"
                )
            )
            context = MigrationContext.configure(connection)
            with Operations.context(context):
                migration.upgrade()

            tokens = connection.execute(
                text(
                    "SELECT purchase_token, professional_id "
                    "FROM subscription_purchase_tokens"
                )
            ).all()
            profiles = connection.execute(
                text(
                    "SELECT id, purchase_token FROM professionals ORDER BY id"
                )
            ).all()
            self.assertEqual(
                tokens,
                [("existing-token", "professional-1")],
            )
            self.assertEqual(
                profiles,
                [
                    ("professional-1", "existing-token"),
                    ("professional-2", None),
                ],
            )

            with Operations.context(context):
                migration.downgrade()
            self.assertNotIn(
                "subscription_purchase_tokens",
                inspect(connection).get_table_names(),
            )
        engine.dispose()


if __name__ == "__main__":
    unittest.main()
