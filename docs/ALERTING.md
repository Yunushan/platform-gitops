# Alerting and SLO Runbook

This runbook defines the minimum alerting contract for a production deployment
of the platform. It does not contain private receiver URLs, phone numbers,
tokens, or escalation contacts. Keep those in the private deployment repository
or the organization's incident tooling.

## Alerting Principles

- Alerts must be actionable.
- Every paging alert must have an owner and a runbook.
- Alert ownership should map to service catalog entries in `docs/SERVICE_CATALOG.md`.
- Alert routing must be tested before production use.
- Warning alerts should create work, not wake people up.
- Critical alerts should page only when user impact, data loss risk, or
  platform recovery risk is real.
- Silences must be time-boxed and tied to a change or incident record.
- Receiver credentials belong in Kubernetes Secrets, SOPS, External Secrets,
  Vault/OpenBao, or the organization's alerting provider, never in plaintext Git.

## Severity Model

| Severity | Meaning | Response target |
|---|---|---|
| critical | User-facing outage, data loss risk, Kubernetes API loss, registry unavailable for production deploys, or backup/restore protection lost | Immediate page |
| warning | Degraded redundancy, capacity pressure, delayed sync, stale backup, or failed noncritical job | Same business day |
| info | Audit, drift, advisory, or follow-up item | Triage during routine operations |

Map these severities to the incident model in `docs/OPERATIONS.md` and the
response workflow in `docs/INCIDENT_RESPONSE.md`.

## Required Receivers

Production Alertmanager routing should have at least:

- Platform critical receiver.
- Platform warning receiver.
- Security or secret-rotation receiver.
- Backup and restore receiver.
- Null or drop receiver for explicitly ignored lab/test alerts.

Receiver endpoints and credentials are private. Store them in a Secret such as
`monitoring/alertmanager-main` or the name selected by the private monitoring
overlay. Do not commit webhook URLs, SMTP passwords, chat tokens, or pager
integration keys to this repository.

## Required Platform Signals

The monitoring stack should alert on these production-critical signals:

| Area | Minimum signal |
|---|---|
| Kubernetes API | API unavailable, etcd quorum risk, node not ready |
| CNI/service path | CoreDNS unavailable, Cilium unavailable, kube-proxy path failing when kube-proxy is used |
| Ingress/VIP | Traefik unavailable, app VIP/FQDN unreachable, HTTP-to-HTTPS redirect failure |
| GitOps | Argo CD app Degraded/Unknown, app OutOfSync too long, active operation stuck |
| Git forge | Forgejo/Gitea/GitLab pod unavailable, repository storage pressure |
| CI | Woodpecker server unavailable, agents unavailable, queue stuck |
| Registry | Harbor core/registry unavailable, `/v2/` API failing, registry storage pressure |
| Database | CloudNativePG primary unavailable, replication lag, WAL archive failure, backup failure |
| Storage | Longhorn node not schedulable, degraded/faulted volume, low disk space |
| Observability | Prometheus unavailable, Alertmanager unavailable, Grafana unavailable, Loki unavailable |
| Backups | Velero BackupStorageLocation unavailable, backup schedule missing or failing, stale restore drill evidence |
| Certificates | cert-manager Certificate not ready, trust-manager Bundle not ready, step-ca `/health` failing when enabled |
| Secrets | required generated app secret missing, SOPS/external-secret sync failure |

`make platform-app-health` covers many of these as an on-demand gate. Alerting
should make the same kinds of failures visible without waiting for a manual run.

### Independent HTTPS checks

Run Prometheus, Blackbox Exporter and Alertmanager on a Linux host that can
reach the platform endpoints and the incident receiver during a cluster outage.
Copy the [example plan](../examples/external-monitoring.json) into ignored
private storage, then set the DNS names, exact expected response codes and
alert owners. An expected HTTP 401 verifies the TLS connection and
expected unauthorized response for a protected endpoint. Choose a dedicated
health endpoint when the check needs to verify service readiness.

```bash
python3 scripts/render_external_monitoring.py \
  --plan private/external-monitoring.json --runtime raw \
  --output-dir private/rendered-external-monitoring
```

Install the configs in `/etc/platform-monitoring` and provide
`probe-ca.pem`, `receiver-ca.pem`, and `webhook-url` there. Put the HTTPS
receiver URL alone in `webhook-url`. Restrict it to the Alertmanager service
account and use a receiver controlled by the organization. Run the native
programs under unprivileged supervised accounts with writable state directories.
Set these executable arguments in the supervisor and create the state
directories with ownership assigned to their respective service accounts.

```bash
blackbox_exporter --web.listen-address=127.0.0.1:9115 \
  --config.file=/etc/platform-monitoring/blackbox.yaml
alertmanager --web.listen-address=127.0.0.1:9093 --cluster.listen-address= \
  --config.file=/etc/platform-monitoring/alertmanager.yaml \
  --storage.path=/var/lib/platform-monitoring/alerts
prometheus --web.listen-address=127.0.0.1:9090 \
  --config.file=/etc/platform-monitoring/prometheus.yaml \
  --storage.tsdb.path=/var/lib/platform-monitoring/metrics \
  --storage.tsdb.retention.time=15d
```

For Docker, add an `images` object with digest-pinned `prometheus`,
`blackbox`, and `alertmanager` images and render with `--runtime docker`.
Place the same three private input files and separate `prometheus-state`
and `alertmanager-state` directories alongside `compose.yaml`. The configs
and private inputs must be readable by UID 65534, and the state directories
must be writable by that UID. On a rootful Docker host without user
namespace remapping, prepare the inputs and start the rendered project
from its output directory.

```bash
sudo chown 65534:65534 prometheus.yaml alerts.yaml blackbox.yaml \
  alertmanager.yaml probe-ca.pem receiver-ca.pem webhook-url
sudo chmod 0400 prometheus.yaml alerts.yaml blackbox.yaml \
  alertmanager.yaml probe-ca.pem receiver-ca.pem webhook-url
sudo install -d -o 65534 -g 65534 -m 0700 prometheus-state alertmanager-state
sudo docker compose config --quiet
sudo docker compose up -d
```

For a remapped Docker daemon, use the host IDs that correspond to UID and
GID 65534 inside its containers. The Compose network carries the internal
scrapes and alerts. Keep the Blackbox Exporter probe endpoint private.

The monitor compares exact HTTPS status codes, verifies TLS, and refuses
redirects. It sends a critical alert when a probe fails for 30 seconds and
a warning when an endpoint's probe series disappears for 30 seconds.
Alertmanager sends recovery notifications to the same receiver.
[Prometheus multi-target exporter pattern](https://prometheus.io/docs/guides/multi-target-exporter/)

Disposable Linux tests with Prometheus 2.53.5, Blackbox Exporter 0.26.0,
and Alertmanager 0.28.1 exercised both native programs and the digest-pinned
upstream images in Compose. A local TLS endpoint and webhook verified an
expected registry 401 and both firing and resolved alerts for endpoint
failure and exporter loss. In Compose, Prometheus replayed historical probe
data after a restart, and Alertmanager saved its notification log on shutdown.

## SLO and Error Budget Expectations

Each private deployment should define service objectives for:

- Kubernetes API availability.
- App ingress VIP/FQDN availability.
- Argo CD reconciliation freshness.
- CI queue latency.
- Registry pull availability.
- Backup freshness.
- Restore drill freshness.

At minimum, record:

```text
Service:
Objective:
Measurement:
Window:
Owner:
Alert threshold:
Runbook:
```

When the error budget is exhausted, freeze nonessential platform changes until
the owner records a mitigation or risk acceptance.

## Alert Routing Tests

Before production use and after receiver changes:

1. Apply the private Alertmanager routing configuration.
2. Send a test alert for each receiver.
3. Confirm the notification arrives in the expected channel.
4. Confirm critical alerts page the expected escalation path.
5. Confirm warning alerts do not page unless intentionally configured.
6. Confirm silences suppress only the intended labels.
7. Record the test date and operator in private evidence.

## Silences and Maintenance

Create silences only for planned work or known incidents. Every silence should
include:

- Owner.
- Reason.
- Change or incident reference.
- Start and end time.
- Exact matchers.

Do not use broad namespace-wide or severity-wide silences unless the incident
commander approves them.

## Alert Review

Review alerts monthly:

- Remove alerts that never require action.
- Tighten alerts that fire too late.
- Add alerts for incidents that were detected by users before monitoring.
- Check noisy alerts for missing thresholds, missing `for` durations, or bad
  label routing.
- Verify every critical alert still has a current owner and runbook.

## Production Evidence

Keep private evidence for:

- Latest Alertmanager receiver test.
- Current receiver routing map.
- Current severity mapping.
- Current SLO/error budget definitions.
- Monthly alert review.
- Silences created during incidents or maintenance.
- Alert gaps discovered during incidents and the follow-up fix.

Do not commit private receiver details or escalation contacts to this public
template.
