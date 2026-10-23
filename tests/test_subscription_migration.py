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
WEBHOOK_MIGRATION_PATH = (
    Path(__file__).parents[1]
    / "migrations"
    / "versions"
    / "20261023_google_play_webhook_events.py"
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


class GooglePlayWebhookMigrationTests(unittest.TestCase):
    def test_webhook_event_migration_creates_and_drops_inbox(self):
        spec = importlib.util.spec_from_file_location(
            "google_play_webhook_events_migration",
            WEBHOOK_MIGRATION_PATH,
        )
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)

        engine = create_engine("sqlite://")
        with engine.begin() as connection:
            context = MigrationContext.configure(connection)
            with Operations.context(context):
                migration.upgrade()

            inspector = inspect(connection)
            self.assertIn(
                "google_play_webhook_events",
                inspector.get_table_names(),
            )
            self.assertEqual(
                {column["name"] for column in inspector.get_columns(
                    "google_play_webhook_events"
                )},
                {
                    "event_key",
                    "purchase_token_hash",
                    "notification_type",
                    "event_time_millis",
                    "status",
                    "received_at",
                    "processed_at",
                    "attempt_count",
                },
            )
            self.assertEqual(
                {index["name"] for index in inspector.get_indexes(
                    "google_play_webhook_events"
                )},
                {
                    "ix_google_play_webhook_events_purchase_token_hash",
                    "ix_google_play_webhook_events_status",
                    "ix_google_play_webhook_events_received_at",
                },
            )

            with Operations.context(context):
                migration.downgrade()
            self.assertNotIn(
                "google_play_webhook_events",
                inspect(connection).get_table_names(),
            )
        engine.dispose()


if __name__ == "__main__":
    unittest.main()
