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
`professionals.purchase_token` values. The migration
`20261023_google_play_webhook_events` adds an idempotent inbox for RTDN events
that arrive before a purchase can be securely linked to a professional. Neither
migration modifies patients, appointments, or clinical records.

Back up the database and review the migration plan before deployment. Apply the
migrations before deploying application code that queries
`subscription_purchase_tokens` and `google_play_webhook_events`; do not run them
implicitly on application startup. Downgrading the webhook inbox removes
pending-event and deduplication state. Downgrades should only be used when that
data can be discarded or has been backed up.

If `subscription_purchase_tokens` was created manually in Neon rather than by
Alembic, first compare the actual schema with `alembic_version` and reconcile
the migration history deliberately. Do not run `alembic upgrade head` or stamp a
revision blindly: the earlier token migration creates that table and can fail
if it already exists.

Coordinate deployment with the Android application. The verification endpoint
now returns the same profile representation as login/profile (including
`planType`, `subscriptionStatus`, `trialEndsAt`, `subscriptionExpiresAt`,
`expirationDate`, `isSubscriptionExpired`, and `bloquear_app`), rather than the
previous small status/message object. Android should parse the response, keep the
purchase token for retry after network errors, and acknowledge the purchase only
after server verification succeeds.

`trialEndsAt` and `subscriptionExpiresAt` are full ISO 8601 timestamps
normalized to UTC (for example `2026-10-23T04:15:30Z`). `expirationDate`
remains a legacy UTC date-only field for older clients. Updated clients must use
the full timestamp fields for expiration decisions. Deploy the compatible
Android parser before or together with this API contract change.

## Subscription behavior

- Product IDs map to `MONTHLY`, `QUARTERLY`, `SEMIANNUAL`, and `ANNUAL`; plan and
  expiration are read from Google Play, not calculated from client input.
- `ACTIVE` and Google `IN_GRACE_PERIOD` are represented as `ACTIVE` and permit
  access until the Google expiry time.
- `ON_HOLD`, `PAUSED`, `PENDING`, and `EXPIRED` do not grant paid access.
  A pending verification does not replace an existing valid subscription.
- Trial access is checked against `trial_ends_at`; paid access is checked
  against `subscription_expires_at`. Access expires when the current UTC time is
  greater than or equal to the applicable expiry.
- `CANCELED` means renewal was cancelled. The user retains paid access until the
  exact Google Play expiry time; cancellation alone does not revoke access.
- Unknown Google states or product IDs fail closed and require investigation.

## RTDN inbox and reconciliation

If a webhook cannot yet be matched using the Google obfuscated account ID or an
already-associated current/linked purchase token, the API stores a minimal
pending inbox row and acknowledges the Pub/Sub delivery with HTTP 200. It does
not infer ownership from email, CPF, product, or arrival order. The inbox stores
only a SHA-256 fingerprint of the purchase token, event metadata, processing
state, timestamps, and attempt count; the raw token is not copied into this
table. Existing subscription-token tables remain sensitive and must have
restricted database access.

When an authenticated `/api/v1/subscription/verify` call securely associates
the purchase token, linked pending notifications are reconciled in the same
database transaction as the latest verified subscription state. Reconciliation
uses the fresh Google Play response from that verification, not the possibly
stale RTDN payload. Webhooks whose pending token is already associated resume
processing on a subsequent delivery. Transient failures before an event is
durably stored return a 5xx so Pub/Sub can retry; failures after persistence
leave the event pending for a later authenticated verification or delivery.
Permanent identity/token conflicts and unrecognized purchase tokens are recorded
as `REJECTED` and acknowledged with HTTP 200, avoiding endless Pub/Sub retries;
they never change account ownership or subscription state.

Operational checks (do not select or export token fingerprints):

```sql
SELECT status, COUNT(*) AS event_count,
       MIN(received_at) AS oldest_received_at
FROM google_play_webhook_events
GROUP BY status;

SELECT event_key, notification_type, event_time_millis, received_at,
       attempt_count
FROM google_play_webhook_events
WHERE status = 'PENDING'
ORDER BY received_at;
```

Alert on pending events older than 24 hours and investigate the association
through the authenticated app verification flow. Pending records are not
automatically deleted so unresolved purchases are not silently lost; review and
explicitly clean them only after resolving or documenting the case. Processed
and rejected rows contain no token fingerprint and are retained for 90 days for
Pub/Sub deduplication. Schedule this cleanup through a reviewed database
maintenance job:

```sql
DELETE FROM google_play_webhook_events
WHERE status IN ('PROCESSED', 'REJECTED')
  AND processed_at < NOW() - INTERVAL '90 days';
```

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
