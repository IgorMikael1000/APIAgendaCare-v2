import base64
import json
import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import main
import models
import schemas
from database import Base, get_db


class SubscriptionFlowTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.session_factory = sessionmaker(
            autocommit=False,
            autoflush=False,
            bind=self.engine,
        )
        self.db = self.session_factory()
        self.professional = models.ProfessionalModel(
            id="professional-1",
            firebase_uid="firebase-user-1",
            plan_type="FREE",
            trial_ends_at=datetime.now(timezone.utc) + timedelta(days=2),
            subscription_status="TRIAL",
            name="Test User",
            email="test@example.com",
            specialty="Fonoaudiologia",
            cpf="12345678901",
        )
        self.db.add(self.professional)
        self.db.commit()

        def override_db():
            yield self.db

        def override_current_user():
            return (
                self.db.query(models.ProfessionalModel)
                .filter_by(id=self.professional.id)
                .one()
            )

        main.app.dependency_overrides[get_db] = override_db
        main.app.dependency_overrides[
            main.get_current_user
        ] = override_current_user
        main.app.dependency_overrides[
            main.verify_google_play_pubsub_token
        ] = lambda: None
        self.client = TestClient(main.app)
        self.package_patch = patch.dict(
            os.environ,
            {main.GOOGLE_PLAY_PACKAGE_ENV: "app.itsolutions.agendacare"},
        )
        self.package_patch.start()

    def tearDown(self):
        self.package_patch.stop()
        main.app.dependency_overrides.clear()
        self.db.close()
        Base.metadata.drop_all(self.engine)
        self.engine.dispose()

    def make_subscription(
        self,
        state="SUBSCRIPTION_STATE_ACTIVE",
        product_id="plan_monthly",
        expiry=None,
        account_id="firebase-user-1",
        linked_token=None,
    ):
        if expiry is None and state != "SUBSCRIPTION_STATE_PENDING":
            expiry = datetime.now(timezone.utc) + timedelta(days=29)
        return schemas.GooglePlaySubscriptionDetails(
            subscriptionState=state,
            linkedPurchaseToken=linked_token,
            externalAccountIdentifiers=(
                schemas.GooglePlayExternalAccountIdentifiers(
                    obfuscatedExternalAccountId=account_id
                )
                if account_id is not None
                else None
            ),
            lineItems=[
                schemas.GooglePlayLineItem(
                    productId=product_id,
                    expiryTime=expiry,
                )
            ],
        )

    def verify(self, token="purchase-token-1", plan="MONTHLY"):
        return self.client.post(
            "/api/v1/subscription/verify",
            json={"purchaseToken": token, "planType": plan},
        )

    def post_webhook(self, notification, package_name=None):
        payload = {
            "version": "1.0",
            "packageName": package_name or "app.itsolutions.agendacare",
            "eventTimeMillis": "1780000000000",
            **notification,
        }
        message = {
            "data": base64.b64encode(
                json.dumps(payload).encode("utf-8")
            ).decode("ascii"),
            "messageId": "test-event-1",
        }
        return self.client.post(
            "/api/v1/webhooks/google-play",
            json={"message": message},
        )

    def test_verification_uses_google_product_and_expiry_in_profile_response(self):
        expiry = datetime.now(timezone.utc) + timedelta(days=25)
        subscription = self.make_subscription(
            product_id="plan_monthly",
            expiry=expiry,
        )
        with patch.object(
            main,
            "fetch_google_play_subscription",
            return_value=subscription,
        ) as fetch:
            response = self.verify()

        self.assertEqual(response.status_code, 200, response.text)
        profile = response.json()
        self.assertEqual(profile["planType"], "MONTHLY")
        self.assertEqual(profile["subscriptionStatus"], "ACTIVE")
        self.assertEqual(
            profile["subscriptionExpiresAt"],
            expiry.date().isoformat(),
        )
        self.assertFalse(profile["isSubscriptionExpired"])
        self.assertFalse(profile["bloquear_app"])
        self.assertEqual(fetch.call_args.args[1], "app.itsolutions.agendacare")

        stored = self.db.query(models.ProfessionalModel).one()
        self.assertEqual(stored.purchase_token, "purchase-token-1")
        self.assertEqual(
            stored.subscription_expires_at,
            expiry.replace(tzinfo=None),
        )
        self.assertEqual(
            self.db.query(models.SubscriptionPurchaseTokenModel).count(),
            1,
        )

    def test_every_supported_product_maps_from_google_response(self):
        products = {
            "plan_monthly": "MONTHLY",
            "plan_quarterly": "QUARTERLY",
            "plan_semiannual": "SEMIANNUAL",
            "plan_annual": "ANNUAL",
        }
        for index, (product_id, expected_plan) in enumerate(products.items()):
            with self.subTest(product_id=product_id):
                subscription = self.make_subscription(
                    product_id=product_id,
                )
                with patch.object(
                    main,
                    "fetch_google_play_subscription",
                    return_value=subscription,
                ):
                    response = self.verify(
                        token=f"purchase-token-{index}",
                        plan=expected_plan,
                    )
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json()["planType"], expected_plan)

    def test_client_plan_mismatch_is_rejected_without_mutating_account(self):
        subscription = self.make_subscription(product_id="plan_monthly")
        with patch.object(
            main,
            "fetch_google_play_subscription",
            return_value=subscription,
        ):
            response = self.verify(plan="ANNUAL")

        self.assertEqual(response.status_code, 409)
        self.db.refresh(self.professional)
        self.assertEqual(self.professional.subscription_status, "TRIAL")
        self.assertIsNone(self.professional.purchase_token)

    def test_unknown_product_is_rejected(self):
        subscription = self.make_subscription(product_id="unknown_product")
        with patch.object(
            main,
            "fetch_google_play_subscription",
            return_value=subscription,
        ):
            response = self.verify()

        self.assertEqual(response.status_code, 502)
        self.db.refresh(self.professional)
        self.assertEqual(self.professional.subscription_status, "TRIAL")

    def test_google_account_mismatch_is_rejected(self):
        subscription = self.make_subscription(account_id="another-user")
        with patch.object(
            main,
            "fetch_google_play_subscription",
            return_value=subscription,
        ):
            response = self.verify()

        self.assertEqual(response.status_code, 403)
        self.db.refresh(self.professional)
        self.assertIsNone(self.professional.purchase_token)

    def test_new_token_without_account_id_or_known_link_is_rejected(self):
        subscription = self.make_subscription(account_id=None)
        with patch.object(
            main,
            "fetch_google_play_subscription",
            return_value=subscription,
        ):
            response = self.verify()

        self.assertEqual(response.status_code, 403)
        self.db.refresh(self.professional)
        self.assertEqual(self.professional.subscription_status, "TRIAL")

    def test_firebase_uid_over_google_limit_is_rejected_before_lookup(self):
        self.professional.firebase_uid = "u" * 65
        self.db.commit()
        with patch.object(main, "fetch_google_play_subscription") as fetch:
            response = self.verify()

        self.assertEqual(response.status_code, 409)
        fetch.assert_not_called()

    def test_pending_purchase_does_not_activate_or_replace_trial(self):
        subscription = self.make_subscription(
            state="SUBSCRIPTION_STATE_PENDING",
            expiry=None,
        )
        with patch.object(
            main,
            "fetch_google_play_subscription",
            return_value=subscription,
        ):
            response = self.verify()

        self.assertEqual(response.status_code, 409)
        self.db.refresh(self.professional)
        self.assertEqual(self.professional.subscription_status, "TRIAL")
        self.assertIsNone(self.professional.purchase_token)

    def test_canceled_purchase_keeps_paid_access_until_google_expiry(self):
        expiry = datetime.now(timezone.utc) + timedelta(days=3)
        subscription = self.make_subscription(
            state="SUBSCRIPTION_STATE_CANCELED",
            expiry=expiry,
        )
        with patch.object(
            main,
            "fetch_google_play_subscription",
            return_value=subscription,
        ):
            response = self.verify()

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["subscriptionStatus"], "CANCELED")
        self.assertFalse(response.json()["isSubscriptionExpired"])
        self.assertFalse(response.json()["bloquear_app"])

    def test_hold_and_paused_states_do_not_grant_paid_access(self):
        for state in (
            "SUBSCRIPTION_STATE_ON_HOLD",
            "SUBSCRIPTION_STATE_PAUSED",
        ):
            with self.subTest(state=state):
                self.professional.purchase_token = "known-token"
                self.professional.subscription_status = "ACTIVE"
                self.db.add(
                    models.SubscriptionPurchaseTokenModel(
                        purchase_token="known-token",
                        professional_id=self.professional.id,
                    )
                )
                self.db.commit()
                subscription = self.make_subscription(
                    state=state,
                    expiry=datetime.now(timezone.utc) + timedelta(days=10),
                )
                with patch.object(
                    main,
                    "fetch_google_play_subscription",
                    return_value=subscription,
                ):
                    response = self.verify(token="known-token")
                self.assertEqual(response.status_code, 200, response.text)
                self.assertTrue(response.json()["isSubscriptionExpired"])
                self.assertTrue(response.json()["bloquear_app"])
                self.professional.purchase_token = None
                self.professional.subscription_status = "TRIAL"
                self.professional.subscription_expires_at = None
                self.db.query(
                    models.SubscriptionPurchaseTokenModel
                ).delete()
                self.db.commit()

    def test_google_lookup_failure_does_not_mutate_subscription(self):
        with patch.object(
            main,
            "fetch_google_play_subscription",
            side_effect=main.HTTPException(
                status_code=502,
                detail="Google Play unavailable",
            ),
        ):
            response = self.verify()

        self.assertEqual(response.status_code, 502)
        self.db.refresh(self.professional)
        self.assertEqual(self.professional.subscription_status, "TRIAL")
        self.assertIsNone(self.professional.purchase_token)

    def test_google_rejected_purchase_token_returns_422_without_logging_token(self):
        token = "private-purchase-token"
        response = Mock()
        error_response = Mock(status_code=404)
        response.raise_for_status.side_effect = main.requests.HTTPError(
            response=error_response
        )
        credential = Mock()

        with (
            patch.dict(
                os.environ,
                {main.GOOGLE_PLAY_SERVICE_ACCOUNT_ENV: "{}"},
            ),
            patch.object(
                main.service_account.Credentials,
                "from_service_account_info",
                return_value=credential,
            ),
            patch.object(main.requests, "get", return_value=response),
            self.assertLogs(main.logger, level="ERROR") as captured_logs,
        ):
            with self.assertRaises(main.HTTPException) as captured_error:
                main.fetch_google_play_subscription(
                    token,
                    "app.itsolutions.agendacare",
                )

        self.assertEqual(
            captured_error.exception.status_code,
            422,
        )
        self.assertTrue(captured_error.exception.__suppress_context__)
        self.assertNotIn(token, "\n".join(captured_logs.output))

    def test_token_owned_by_another_professional_is_rejected_before_lookup(self):
        other = models.ProfessionalModel(
            id="professional-2",
            firebase_uid="firebase-user-2",
            plan_type="FREE",
            subscription_status="TRIAL",
            name="Other User",
            email="other@example.com",
            specialty="Fonoaudiologia",
            cpf="10987654321",
        )
        self.db.add(other)
        self.db.add(
            models.SubscriptionPurchaseTokenModel(
                purchase_token="purchase-token-1",
                professional_id=other.id,
            )
        )
        self.db.commit()

        with patch.object(main, "fetch_google_play_subscription") as fetch:
            response = self.verify()

        self.assertEqual(response.status_code, 409)
        fetch.assert_not_called()
        self.db.refresh(self.professional)
        self.assertIsNone(self.professional.purchase_token)

    def test_linked_token_owned_by_another_professional_is_rejected(self):
        other = models.ProfessionalModel(
            id="professional-2",
            firebase_uid="firebase-user-2",
            plan_type="FREE",
            subscription_status="TRIAL",
            name="Other User",
            email="other@example.com",
            specialty="Fonoaudiologia",
            cpf="10987654321",
        )
        self.db.add(other)
        self.db.add(
            models.SubscriptionPurchaseTokenModel(
                purchase_token="old-token",
                professional_id=other.id,
            )
        )
        self.db.commit()
        subscription = self.make_subscription(linked_token="old-token")
        with patch.object(
            main,
            "fetch_google_play_subscription",
            return_value=subscription,
        ):
            response = self.verify()

        self.assertEqual(response.status_code, 409)
        self.db.refresh(self.professional)
        self.assertIsNone(self.professional.purchase_token)

    def test_repeating_same_verification_is_idempotent(self):
        expiry = datetime.now(timezone.utc) + timedelta(days=25)
        subscription = self.make_subscription(expiry=expiry)
        with patch.object(
            main,
            "fetch_google_play_subscription",
            return_value=subscription,
        ):
            first = self.verify()
            second = self.verify()

        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(second.status_code, 200, second.text)
        self.assertEqual(
            self.db.query(models.SubscriptionPurchaseTokenModel).count(),
            1,
        )
        self.db.refresh(self.professional)
        self.assertEqual(
            self.professional.subscription_expires_at,
            expiry.replace(tzinfo=None),
        )

    def test_purchase_token_is_not_written_to_application_logs(self):
        token = "sensitive-purchase-token"
        subscription = self.make_subscription()
        with patch.object(
            main,
            "fetch_google_play_subscription",
            return_value=subscription,
        ):
            with self.assertLogs("main", level="INFO") as captured:
                response = self.verify(token=token)

        self.assertEqual(response.status_code, 200, response.text)
        self.assertNotIn(token, "\n".join(captured.output))

    def test_historical_token_cannot_overwrite_current_subscription(self):
        current_expiry = datetime.now(timezone.utc) + timedelta(days=20)
        self.professional.plan_type = "ANNUAL"
        self.professional.subscription_status = "ACTIVE"
        self.professional.subscription_expires_at = current_expiry
        self.professional.purchase_token = "current-token"
        self.db.add(
            models.SubscriptionPurchaseTokenModel(
                purchase_token="old-token",
                professional_id=self.professional.id,
            )
        )
        self.db.add(
            models.SubscriptionPurchaseTokenModel(
                purchase_token="current-token",
                professional_id=self.professional.id,
            )
        )
        self.db.commit()

        with patch.object(main, "fetch_google_play_subscription") as fetch:
            response = self.verify(token="old-token", plan="MONTHLY")

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["planType"], "ANNUAL")
        fetch.assert_not_called()
        self.db.refresh(self.professional)
        self.assertEqual(self.professional.purchase_token, "current-token")

    def test_access_policy_for_cancelled_grace_hold_pause_and_expiry(self):
        now = datetime(2026, 1, 10, tzinfo=timezone.utc)
        self.professional.subscription_status = "CANCELED"
        self.professional.subscription_expires_at = now + timedelta(days=1)
        self.assertFalse(main.is_subscription_expired(self.professional, now))
        self.assertTrue(
            main.is_subscription_expired(
                self.professional,
                now + timedelta(days=1),
            )
        )

        self.professional.subscription_status = "ACTIVE"
        self.professional.subscription_expires_at = now + timedelta(days=1)
        self.assertFalse(main.is_subscription_expired(self.professional, now))

        for state in ("ON_HOLD", "PAUSED", "PENDING", "EXPIRED", "UNKNOWN"):
            with self.subTest(state=state):
                self.professional.subscription_status = state
                self.assertTrue(
                    main.is_subscription_expired(self.professional, now)
                )

        self.professional.subscription_status = "TRIAL"
        self.professional.trial_ends_at = now + timedelta(days=1)
        self.assertFalse(main.is_subscription_expired(self.professional, now))
        self.assertTrue(
            main.is_subscription_expired(
                self.professional,
                now + timedelta(days=1),
            )
        )

    def test_google_grace_period_maps_to_active_and_expiration_is_utc(self):
        expiry = datetime(2026, 1, 10, 12, tzinfo=timezone.utc)
        subscription = self.make_subscription(
            state="SUBSCRIPTION_STATE_IN_GRACE_PERIOD",
            expiry=expiry.replace(tzinfo=None),
        )
        result = main.google_play_subscription_snapshot(subscription)
        self.assertEqual(result[0], "ACTIVE")
        self.assertEqual(result[1], "MONTHLY")
        self.assertEqual(result[2], expiry)

    def test_profile_and_verify_share_the_documented_response_model(self):
        operation = main.app.openapi()["paths"][
            "/api/v1/subscription/verify"
        ]["post"]
        schema_ref = operation["responses"]["200"]["content"][
            "application/json"
        ]["schema"]["$ref"]
        self.assertEqual(
            schema_ref,
            "#/components/schemas/ProfessionalResponse",
        )
        profile = main.professional_response(self.professional)
        for key in (
            "planType",
            "subscriptionStatus",
            "trialEndsAt",
            "subscriptionExpiresAt",
            "expirationDate",
            "isSubscriptionExpired",
            "bloquear_app",
        ):
            self.assertIn(key, profile)

        response = self.client.get("/api/v1/auth/profile")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), profile)

    def test_webhook_updates_from_google_and_saves_linked_tokens(self):
        expiry = datetime.now(timezone.utc) + timedelta(days=15)
        subscription = self.make_subscription(
            expiry=expiry,
            linked_token="previous-token",
        )
        notification = {
            "subscriptionNotification": {
                "version": "1.0",
                "notificationType": 2,
                "purchaseToken": "webhook-token",
                "subscriptionId": "plan_monthly",
            }
        }
        with patch.object(
            main,
            "fetch_google_play_subscription",
            return_value=subscription,
        ) as fetch:
            response = self.post_webhook(notification)

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["result"], "subscription_synchronized")
        fetch.assert_called_once_with(
            "webhook-token",
            "app.itsolutions.agendacare",
        )
        self.db.refresh(self.professional)
        self.assertEqual(self.professional.subscription_status, "ACTIVE")
        self.assertEqual(self.professional.purchase_token, "webhook-token")
        self.assertEqual(
            self.db.query(models.SubscriptionPurchaseTokenModel)
            .filter_by(professional_id=self.professional.id)
            .count(),
            2,
        )

    def test_duplicate_webhook_is_idempotent(self):
        expiry = datetime.now(timezone.utc) + timedelta(days=15)
        subscription = self.make_subscription(expiry=expiry)
        notification = {
            "subscriptionNotification": {
                "version": "1.0",
                "notificationType": 2,
                "purchaseToken": "repeat-token",
                "subscriptionId": "plan_monthly",
            }
        }
        with patch.object(
            main,
            "fetch_google_play_subscription",
            return_value=subscription,
        ):
            first = self.post_webhook(notification)
            second = self.post_webhook(notification)

        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(second.status_code, 200, second.text)
        self.assertEqual(
            self.db.query(models.SubscriptionPurchaseTokenModel).count(),
            1,
        )
        self.db.refresh(self.professional)
        self.assertEqual(self.professional.purchase_token, "repeat-token")
        self.assertEqual(
            self.professional.subscription_expires_at,
            expiry.replace(tzinfo=None),
        )

    def test_pending_webhook_does_not_replace_existing_trial(self):
        subscription = self.make_subscription(
            state="SUBSCRIPTION_STATE_PENDING",
            expiry=None,
        )
        notification = {
            "subscriptionNotification": {
                "version": "1.0",
                "notificationType": 4,
                "purchaseToken": "pending-token",
                "subscriptionId": "plan_monthly",
            }
        }
        with patch.object(
            main,
            "fetch_google_play_subscription",
            return_value=subscription,
        ):
            response = self.post_webhook(notification)

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["result"], "purchase_pending")
        self.db.refresh(self.professional)
        self.assertEqual(self.professional.subscription_status, "TRIAL")
        self.assertIsNone(self.professional.purchase_token)

    def test_old_webhook_event_refreshes_latest_token_instead_of_regressing(self):
        self.professional.subscription_status = "ACTIVE"
        self.professional.plan_type = "ANNUAL"
        self.professional.purchase_token = "latest-token"
        self.professional.subscription_expires_at = (
            datetime.now(timezone.utc) + timedelta(days=40)
        )
        for token in ("old-token", "latest-token"):
            self.db.add(
                models.SubscriptionPurchaseTokenModel(
                    purchase_token=token,
                    professional_id=self.professional.id,
                )
            )
        self.db.commit()

        stale = self.make_subscription(
            state="SUBSCRIPTION_STATE_EXPIRED",
            product_id="plan_monthly",
            expiry=datetime.now(timezone.utc) - timedelta(days=1),
        )
        latest = self.make_subscription(
            product_id="plan_annual",
            expiry=datetime.now(timezone.utc) + timedelta(days=40),
        )
        notification = {
            "subscriptionNotification": {
                "version": "1.0",
                "notificationType": 13,
                "purchaseToken": "old-token",
                "subscriptionId": "plan_monthly",
            }
        }
        with patch.object(
            main,
            "fetch_google_play_subscription",
            side_effect=[stale, latest],
        ) as fetch:
            response = self.post_webhook(notification)

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(fetch.call_count, 2)
        self.db.refresh(self.professional)
        self.assertEqual(self.professional.purchase_token, "latest-token")
        self.assertEqual(self.professional.plan_type, "ANNUAL")
        self.assertEqual(self.professional.subscription_status, "ACTIVE")

    def test_webhook_rejects_wrong_package_without_google_lookup(self):
        notification = {
            "subscriptionNotification": {
                "version": "1.0",
                "notificationType": 2,
                "purchaseToken": "purchase-token",
                "subscriptionId": "plan_monthly",
            }
        }
        with patch.object(main, "fetch_google_play_subscription") as fetch:
            response = self.post_webhook(
                notification,
                package_name="another.package",
            )
        self.assertEqual(response.status_code, 400)
        fetch.assert_not_called()

    def test_webhook_acknowledges_valid_test_notification_without_mutation(self):
        notification = {"testNotification": {"version": "1.0"}}
        with patch.object(main, "fetch_google_play_subscription") as fetch:
            response = self.post_webhook(notification)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["result"], "test_notification")
        fetch.assert_not_called()
        self.assertEqual(self.professional.subscription_status, "TRIAL")

    def test_webhook_unknown_professional_returns_retryable_error(self):
        subscription = self.make_subscription(account_id="unknown-user")
        notification = {
            "subscriptionNotification": {
                "version": "1.0",
                "notificationType": 2,
                "purchaseToken": "orphan-token",
                "subscriptionId": "plan_monthly",
            }
        }
        with patch.object(
            main,
            "fetch_google_play_subscription",
            return_value=subscription,
        ):
            response = self.post_webhook(notification)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.headers.get("retry-after"), "60")

    def test_pubsub_oidc_token_requires_expected_audience_and_identity(self):
        with patch.dict(
            os.environ,
            {
                main.GOOGLE_PLAY_PUBSUB_AUDIENCE_ENV: (
                    "https://api.example.com/api/v1/webhooks/google-play"
                ),
                main.GOOGLE_PLAY_PUBSUB_SERVICE_ACCOUNT_ENV: (
                    "pubsub-push@example.iam.gserviceaccount.com"
                ),
            },
        ):
            credentials = HTTPAuthorizationCredentials(
                scheme="Bearer",
                credentials="opaque-token",
            )
            claims = {
                "iss": "https://accounts.google.com",
                "email": "pubsub-push@example.iam.gserviceaccount.com",
                "email_verified": True,
            }
            with patch.object(
                main.google_id_token,
                "verify_oauth2_token",
                return_value=claims,
            ) as verify_token:
                self.assertIsNone(
                    main.verify_google_play_pubsub_token(credentials)
                )
            verify_token.assert_called_once()
            self.assertEqual(
                verify_token.call_args.kwargs["audience"],
                "https://api.example.com/api/v1/webhooks/google-play",
            )

    def test_pubsub_oidc_token_rejects_wrong_service_account(self):
        with patch.dict(
            os.environ,
            {
                main.GOOGLE_PLAY_PUBSUB_AUDIENCE_ENV: (
                    "https://api.example.com/webhook"
                ),
                main.GOOGLE_PLAY_PUBSUB_SERVICE_ACCOUNT_ENV: (
                    "pubsub-push@example.iam.gserviceaccount.com"
                ),
            },
        ):
            credentials = HTTPAuthorizationCredentials(
                scheme="Bearer",
                credentials="opaque-token",
            )
            with patch.object(
                main.google_id_token,
                "verify_oauth2_token",
                return_value={
                    "iss": "https://accounts.google.com",
                    "email": "attacker@example.com",
                    "email_verified": True,
                },
            ):
                with self.assertRaises(main.HTTPException) as raised:
                    main.verify_google_play_pubsub_token(credentials)
        self.assertEqual(raised.exception.status_code, 403)

    def test_pubsub_oidc_requires_a_bearer_token(self):
        with patch.dict(
            os.environ,
            {
                main.GOOGLE_PLAY_PUBSUB_AUDIENCE_ENV: (
                    "https://api.example.com/webhook"
                ),
                main.GOOGLE_PLAY_PUBSUB_SERVICE_ACCOUNT_ENV: (
                    "pubsub-push@example.iam.gserviceaccount.com"
                ),
            },
        ):
            with self.assertRaises(main.HTTPException) as raised:
                main.verify_google_play_pubsub_token(None)
        self.assertEqual(raised.exception.status_code, 401)

    def test_pubsub_oidc_network_failure_is_retryable(self):
        with patch.dict(
            os.environ,
            {
                main.GOOGLE_PLAY_PUBSUB_AUDIENCE_ENV: (
                    "https://api.example.com/webhook"
                ),
                main.GOOGLE_PLAY_PUBSUB_SERVICE_ACCOUNT_ENV: (
                    "pubsub-push@example.iam.gserviceaccount.com"
                ),
            },
        ):
            credentials = HTTPAuthorizationCredentials(
                scheme="Bearer",
                credentials="opaque-token",
            )
            with patch.object(
                main.google_id_token,
                "verify_oauth2_token",
                side_effect=main.google.auth.exceptions.TransportError(
                    "network unavailable"
                ),
            ):
                with self.assertRaises(main.HTTPException) as raised:
                    main.verify_google_play_pubsub_token(credentials)
        self.assertEqual(raised.exception.status_code, 503)


if __name__ == "__main__":
    unittest.main()
