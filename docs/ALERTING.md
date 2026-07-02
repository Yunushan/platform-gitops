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
