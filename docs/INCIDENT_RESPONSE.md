# Incident Response Runbook

This runbook defines a public-safe incident response workflow for private
platform deployments. Do not commit real incident channels, phone numbers,
customer names, IP addresses, domains, logs, screenshots, exploit details, or
production timelines to this public template. Keep those records in the private
deployment repository, incident system, or security case system.

## Principles

- Protect people and data first.
- Stabilize before optimizing.
- Assign clear roles before running risky commands.
- Prefer reversible changes and Git-backed fixes.
- Keep command history, evidence, and decisions in the private incident record.
- Communicate facts, impact, actions, and next update time.
- Rotate exposed credentials quickly; do not wait for root cause.
- Reconcile all manual cluster changes back to Git after recovery.

## Severity Declaration

Use the private deployment severity model, but keep this minimum mapping:

| Severity | Declare when | Initial response |
|---|---|---|
| SEV1 | Kubernetes API unavailable, data loss risk, backup/restore protection lost, registry unavailable for production deploys, or widespread platform outage | Page incident commander and platform owners immediately |
| SEV2 | Argo CD, CI, Git forge, registry, storage redundancy, database primary, or ingress path degraded with production risk | Assign incident commander and start a private incident record |
| SEV3 | Single service route broken, noncritical alert storm, delayed sync, capacity warning, or isolated node issue | Track as operational incident or urgent change |

If the severity is uncertain, start higher and downgrade after evidence is
collected.

## Roles

Assign these roles for SEV1 and SEV2 incidents:

| Role | Responsibility |
|---|---|
| Incident commander | Owns severity, priorities, decisions, and handoff |
| Operations lead | Runs Kubernetes, storage, network, and platform commands |
| Communications lead | Sends stakeholder updates through private channels |
| Scribe | Records timeline, commands, evidence, and decisions |
| Security lead | Handles suspected compromise, secret exposure, audit scope, and evidence preservation |
| Service owner | Confirms application impact and acceptance after recovery |

One person can hold multiple roles during a small incident, but the incident
commander should be explicit about it.

## First 15 Minutes

1. Declare severity and open the private incident record.
2. Assign incident commander, operations lead, communications lead, and scribe.
3. Identify impact, affected services, and current customer/user visibility.
4. Freeze nonessential deployments and risky automated changes when needed.
5. Run the narrowest safe health gate for the failing layer.
6. Preserve volatile evidence before restarting or deleting resources.
7. Send the first private stakeholder update with next update time.

Useful safe starting points:

```bash
make platform-status
make platform-app-health
PLATFORM_PROFILE=<PROFILE> make platform-production-check
```

Use focused targets when the failure is already localized.

## Stabilization Actions

Choose the least destructive action that protects data:

- Pause Argo CD automated sync only when reconciliation is worsening impact.
- Disable a CI runner, token, or workflow when CI is suspected of causing or
  spreading the incident.
- Preserve pod logs and events before deleting pods.
- Prefer scaling down a failing integration over deleting persistent data.
- Do not reset PVCs, PVs, databases, object storage, or certificates without
  explicit incident commander approval and restore evidence.
- Treat break-glass access as temporary and record commands touched.
- If secret exposure is suspected, rotate affected credentials and follow
  `SECURITY.md`.

## Component Triage Matrix

| Area | First evidence | Escalate to |
|---|---|---|
| Kubernetes API and etcd | Node readiness, API reachability, etcd snapshot status | RKE2/control-plane owner |
| CNI, CoreDNS, kube-proxy, and service path | CoreDNS pods, Cilium status, ClusterIP probes, service endpoints | Network/platform owner |
| Ingress and VIP | MetalLB pool, Traefik pods, endpoint coverage, FQDN/VIP curl checks | Network/ingress owner |
| Argo CD | Application states, repo-server logs, controller logs, active operations | GitOps owner |
| Forgejo, Gitea, or GitLab | Pod readiness, repository storage, database, login and clone tests | Source-control owner |
| Woodpecker CI | Server health, agent health, queue depth, OAuth and secret status | CI owner |
| Harbor | Core, registry, portal, database, Redis, `/v2/` checks, storage usage | Registry owner |
| CloudNativePG | Cluster phase, primary availability, replication, WAL archive, backup status | Database owner |
| Longhorn or alternate storage | Volume health, node schedulability, disk pressure, replica count | Storage owner |
| Velero and backups | BackupStorageLocation, latest backup, restore drill age | Disaster recovery owner |
| Prometheus, Grafana, Loki | Metrics scrape health, Alertmanager, dashboards, log ingestion | Observability owner |
| cert-manager, trust-manager, step-ca | Certificate readiness, Bundle readiness, CA health and key access | PKI/security owner |

## Communications

Private incident updates should include:

```text
Incident ID:
Severity:
Impact:
Affected services:
Current status:
Actions completed:
Next action:
Next update time:
Known risks:
Help needed:
```

Do not post private logs, screenshots, credentials, customer identifiers, or
internal topology in public issues or public pull requests.

## Evidence Collection

Collect only the evidence needed to understand and recover:

- Alert name, firing time, labels, and receiver.
- Recent Argo CD application state.
- Relevant Kubernetes events and workload status.
- Safe excerpts of logs with secrets redacted.
- Recent Git commits, pull requests, syncs, releases, or CI runs.
- Storage, database, backup, registry, and ingress health for affected layers.
- Commands run, who ran them, and why.

Keep raw evidence private. Redact before sharing outside the incident team.

## Recovery Validation

Before closing the incident:

1. Confirm affected users or services are healthy.
2. Run the targeted health gate for the failed component.
3. Run `make platform-status`.
4. Run `make platform-app-health` for broad platform impact.
5. Confirm Argo CD is not hiding live drift.
6. Confirm paused syncs, silences, and temporary firewall/routing changes are
   removed or tracked.
7. Confirm credentials were rotated when exposure was possible.
8. Send a private recovery update with remaining risks and follow-up owners.

## Post-Incident Review

Run a blameless review for SEV1 and SEV2 incidents, and for any SEV3 that
revealed a systemic gap.

Private review template:

```text
Incident ID:
Date/time:
Severity:
Incident commander:
Services affected:
Customer/user impact:
Detection source:
Timeline:
Root cause:
Contributing factors:
What went well:
What went poorly:
Data or secret exposure:
Backups/restores involved:
Manual changes made:
Git follow-up required:
Monitoring gaps:
Runbook gaps:
Preventive actions:
Owners and due dates:
```

Follow-up actions should become pull requests, tickets, alert changes, restore
drills, access reviews, or threat-model updates.

## Production Evidence

Keep private evidence for:

- Incident declaration and severity.
- Role assignments and handoff notes.
- Timeline, commands, and decisions.
- Alerts, health checks, and recovery validation.
- Credential rotation when applicable.
- Post-incident review and follow-up owners.
- Git reconciliation for every manual production change.

Do not commit private incident evidence to this public template.
