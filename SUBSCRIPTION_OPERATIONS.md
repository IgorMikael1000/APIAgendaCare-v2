# Google Play subscription operations

## Required backend configuration

Configure these values through the deployment platform's secret/environment
configuration. Do not put credentials or tokens in source control:

- `GOOGLE_PLAY_PACKAGE_NAME`: the exact published Android application ID,
  currently `app.itsolutions.agendacare`.
- `GOOGLE_PLAY_SERVICE_ACCOUNT_JSON`: service-account JSON with access to the
  Google Play Developer API for this application and permission to read
  subscription purchases.
- `GOOGLE_PLAY_PUBSUB_AUDIENCE`: the exact audience configured on the
  authenticated Pub/Sub push subscription. Use a stable HTTPS URL for the
  webhook, or an explicitly configured audience accepted by the deployment.
- `GOOGLE_PLAY_PUBSUB_SERVICE_ACCOUNT_EMAIL`: the service account Pub/Sub uses
  to sign push OIDC tokens. The API requires this email to match the verified
  `email` claim.

The Pub/Sub push subscription must enable OIDC authentication, use the service
account above, and deliver to `POST /api/v1/webhooks/google-play`. The Pub/Sub
service agent must have permission to mint tokens for that service account.
Configure a dead-letter topic and alerting for repeated delivery failures.

## Data migration and rollout

The migration `20261022_subscription_purchase_tokens` creates the table used to
associate current and linked purchase tokens and backfills it from existing
`professionals.purchase_token` values. It does not modify patients, appointments,
or clinical records.

Back up the database and review the migration plan before deployment. Apply the
migration before deploying application code that queries
`subscription_purchase_tokens`; do not run it implicitly on application startup.
The migration downgrade drops the token-association table, so it should only be
used when that association data can be discarded or has been backed up.

Coordinate deployment with the Android application. The verification endpoint
now returns the same profile representation as login/profile (including
`planType`, `subscriptionStatus`, `trialEndsAt`, `subscriptionExpiresAt`,
`expirationDate`, `isSubscriptionExpired`, and `bloquear_app`), rather than the
previous small status/message object. Android should parse the response, keep the
purchase token for retry after network errors, and acknowledge the purchase only
after server verification succeeds.

## Subscription behavior

- Product IDs map to `MONTHLY`, `QUARTERLY`, `SEMIANNUAL`, and `ANNUAL`; plan and
  expiration are read from Google Play, not calculated from client input.
- `ACTIVE` and Google `IN_GRACE_PERIOD` are represented as `ACTIVE` and permit
  access until the Google expiry time.
- `CANCELED` permits access until the already-paid expiry time; cancellation
  alone does not immediately remove access.
- `ON_HOLD`, `PAUSED`, `PENDING`, and `EXPIRED` do not grant paid access.
  A pending verification does not replace an existing valid subscription.
- Trial access is checked against `trial_ends_at`; paid access is checked
  against `subscription_expires_at`. Access expires when the current UTC time is
  greater than or equal to the applicable expiry.
- Unknown Google states or product IDs fail closed and require investigation.

## Identity and token handling

The Android app currently sends its Firebase UID as Billing's obfuscated account
identifier. Firebase UIDs must be opaque, contain no personally identifiable
information, and fit Google's 64-character limit. Google recommends an encrypted
or one-way-hashed identifier; switching to one requires a coordinated Android
and API rollout because the server currently compares the returned value to the
authenticated Firebase UID. The server compares the identifier returned by
Google to the authenticated Firebase UID. New purchases without a verifiable
account identifier or a previously associated linked token are not attached to
an account. Do not substitute email, CPF, raw purchase tokens, or other personal
data for this identifier.

Purchase tokens are secrets. Never put them in logs, URLs under application
logging, analytics, error messages, or support tickets. Logs should contain only
safe event IDs, status, plan, and result.

## Release verification

Use Google Play license-test accounts and test products before production.
Verify first purchase, pending payment, active renewal, cancellation before
expiry, grace period, hold, pause, expiry, token replacement/upgrade, duplicate
RTDN delivery, an out-of-order old-token event, webhook authentication failure,
and temporary API/database failure. Confirm in Pub/Sub metrics that successful
events are acknowledged and transient failures are retried or sent to the
configured dead-letter topic.
