# Production Observability

The premium profile treats observability as a delivery path rather than only a
set of running pods.

## Logging

Grafana Alloy runs as a two-replica clustered Deployment. It discovers pod logs
through the Kubernetes API, shards targets between replicas, and writes them to
the internal Loki gateway. It does not require privileged containers, host PID,
or host log-directory mounts.

Loki runs in simple scalable mode with three read, write, and backend replicas.
The gateway has three replicas, requires basic authentication, and overwrites
`X-Scope-OrgID` with the fixed `platform` tenant. The default retention period
is 720 hours (30 days), enforced by the compactor against S3 object storage.

`make platform-app-secrets` generates and preserves:

- `logging/loki-gateway-basic-auth`, including the Nginx `.htpasswd` data.
- `monitoring/platform-loki-client`, used by Alloy and Grafana.

Override the generated credential only through an ignored private environment
file:

```bash
LOKI_GATEWAY_USERNAME=platform
LOKI_GATEWAY_PASSWORD="${PRIVATE_RANDOM_PASSWORD:?set PRIVATE_RANDOM_PASSWORD}"
LOKI_RETENTION_PERIOD=720h
```

Do not commit the password or a password hash to Git values.

## Alert Routing

Alertmanager uses `monitoring/alertmanager-platform-config`. In production
strict mode, secret automation refuses to create a null route. Supply one of:

```bash
ALERTMANAGER_WEBHOOK_URL=https://alerts.example.test/platform
```

or a complete private configuration:

```bash
ALERTMANAGER_CONFIG=<FULL_PRIVATE_ALERTMANAGER_YAML>
```

The webhook shortcut generates grouping, repeat, and resolved-notification
settings. A complete configuration is appropriate when routing by severity,
team, or escalation policy.

Set `PLATFORM_ALERTMANAGER_REQUIRE_ROUTE=false` only in a lab that intentionally
discards notifications.

## Proof

Run:

```bash
make platform-observability-verify
```

The verifier checks HA replica readiness, active retention settings, anonymous
Loki rejection, authenticated push/query, Alloy-collected logs, matching
Grafana client credentials, and Alertmanager configuration validity.

The complete production gate additionally sets
`PLATFORM_ALERT_DELIVERY_TEST=true`. It sends a short-lived informational alert
and requires `alertmanager_notifications_total` to increase without a matching
increase in `alertmanager_notifications_failed_total`. The configured receiver
will therefore receive a notification named `PlatformAlertDeliveryTest` during
an acceptance run.
