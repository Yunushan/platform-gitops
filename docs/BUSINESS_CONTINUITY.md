# Business Continuity and Disaster Recovery

This runbook defines a public-safe business continuity and disaster recovery model
for private platform deployments. Keep real sites, internal contacts,
customer impact, vendor cases, ticket links, IP addresses, topology diagrams,
backup bucket names, and approval records in the private deployment repository
or continuity system.

Use this document with `docs/BACKUP_RESTORE.md`, `docs/INCIDENT_RESPONSE.md`,
`docs/OPERATIONS.md`, `docs/PRODUCTION_READINESS.md`,
`docs/SERVICE_CATALOG.md`, `docs/RELEASE_PROMOTION.md`, and
`docs/COMPLIANCE_AUDIT.md`.

## Continuity Principles

- Continuity is proven by restore drills, dependency tests, and operator
  exercises, not by the existence of backups.
- Protect the minimum viable platform first, then recover optional services.
- Prefer clear manual recovery steps over automatic failover that has not been
  tested.
- Every continuity assumption needs an owner, evidence, review date, and
  expiration.
- A skipped restore drill, stale backup, missing owner, or failed dependency
  check is a production readiness exception.

## Scope

This model covers the platform control plane, GitOps source, ingress, storage,
registry, CI, observability, backups, and PKI services used to recover or
operate workloads.

It does not define application-specific business impact analysis, customer
communications, legal notifications, vendor contract terms, or multi-region
infrastructure design. Keep those details private.

## Minimum Viable Platform

Private deployments should define the smallest platform state that allows safe
operation and recovery:

| Priority | Capability | Evidence |
|---|---|---|
| P0 | RKE2 API, etcd quorum, node readiness, CNI, CoreDNS, and kube-proxy service path | `make rke2-verify`, `make platform-status` |
| P0 | GitOps source of truth reachable from Argo CD | Argo CD repository connection and Application comparison evidence |
| P0 | Backups and restore credentials available | Velero BackupStorageLocation, backup age, restore drill |
| P0 | Storage and database recovery path | Longhorn or alternate storage health, CloudNativePG backup/WAL status |
| P1 | Ingress and VIP routing | API VIP, app VIP, Traefik or alternate ingress checks |
| P1 | Source control and registry | Forgejo/Gitea/GitLab login/clone/push and Harbor `/v2/` checks |
| P1 | CI and release automation | Woodpecker or selected runner queue and agent health |
| P1 | Observability and alerting | Prometheus, Grafana, Loki, Alertmanager, alert receiver test |
| P1 | PKI and trust | cert-manager Certificate readiness, trust-manager Bundle readiness, step-ca health when enabled |

Services outside the minimum viable platform should recover only after the P0
and selected P1 dependencies are stable.

## Dependency Recovery Order

Use this recovery order unless a private incident commander approves a
different sequence:

1. Confirm operator access, break-glass credentials, and private runbooks.
1. Confirm node power, storage, time sync, and network path.
1. Restore or repair RKE2 API and etcd quorum.
1. Restore CNI, CoreDNS, kube-proxy, and ClusterIP service path.
1. Confirm backup target access and restore credentials.
1. Restore storage classes, volume systems, and database operators.
1. Restore Argo CD and connect it to the intended private GitOps source.
1. Restore ingress and VIP routing.
1. Restore source control, registry, CI, observability, and PKI.
1. Resume normal GitOps sync only after the recovered platform passes health
    gates and the incident commander approves.

## Scenario Matrix

| Scenario | First response | Required proof before recovery is accepted |
|---|---|---|
| Single node loss | Keep quorum, drain or replace the node, watch storage replicas | All nodes Ready or replacement accepted, Longhorn or alternate storage healthy |
| Control-plane quorum risk | Freeze nonessential changes, protect etcd and API evidence | Etcd quorum safe, API ready, recent snapshot or restore proof |
| Storage data loss | Stop destructive syncs, preserve evidence, choose restore point | Restore drill or live restore completes inside accepted RTO |
| GitOps source unavailable | Freeze production desired-state changes, use documented temporary source only if approved | Private source restored or approved fallback source recorded |
| Registry unavailable | Freeze image-dependent releases, verify rollback artifacts | Harbor or alternate registry reachable and required artifacts present |
| Backup target unavailable | Stop production launch, protect existing snapshots, restore backup path | BackupStorageLocation Available and backup age inside RPO |
| Ingress/VIP failure | Verify API path separately, test node-local service paths | App VIP and required FQDN checks pass from expected clients |
| PKI or trust failure | Freeze certificate-changing deployments, validate CA ownership | Certificates Ready, Bundles synced, CA health and backup reviewed |
| Region or site loss | Activate private continuity plan and dependency recovery order | Minimum viable platform restored in accepted location with private approval |

## RPO and RTO Model

Use `docs/BACKUP_RESTORE.md` to record concrete RPO and RTO targets. The
continuity record should also identify:

- Maximum accepted time without the platform control plane.
- Maximum accepted time without source control, registry, CI, observability,
  and ingress.
- Maximum accepted data loss for Git repositories, registry artifacts,
  PostgreSQL data, object storage, and Kubernetes state.
- Whether rollback, forward recovery, or restore-from-backup is the expected
  recovery mode for each service.
- Who can accept missed RPO/RTO during an incident.

Do not publish real service-level commitments in this public template.

## Failover and Failback

Failover is allowed only when the private plan identifies:

- Target recovery location or cluster class.
- Required backup source and restore point.
- Required secrets, SOPS recipients, external secret stores, and credentials.
- DNS, VIP, certificate, and trust changes.
- GitOps source and branch or commit to recover.
- Data consistency checks before users return.
- Failback criteria and data reconciliation steps.

Failback should not begin until the recovered environment has a current backup,
the original failure cause is understood, and the rollback or roll-forward path
is approved.

## Continuity Exercises

Run continuity exercises:

- Quarterly for restore and minimum viable platform tabletop.
- Before major RKE2, storage, database, ingress, registry, backup, or PKI
  upgrades.
- After incidents, failed restore drills, failed alert routing tests, or
  material architecture changes.
- Before declaring a new private deployment production-ready.

Each exercise should produce private evidence with operator, date, scenario,
scope, elapsed time, failed assumptions, follow-up owners, and next review date.

## Continuity Evidence

Private deployments should keep evidence for:

- Current minimum viable platform definition.
- Current service catalog ownership and dependency map from `docs/SERVICE_CATALOG.md`.
- Current dependency recovery order and owner list.
- Latest restore drill and RPO/RTO result from `docs/BACKUP_RESTORE.md`.
- Latest incident exercise or tabletop from `docs/INCIDENT_RESPONSE.md`.
- Latest alert receiver test and SLO review from `docs/ALERTING.md`.
- Latest capacity review from `docs/CAPACITY_PLANNING.md`.
- Latest support and lifecycle review from `docs/PLATFORM_SUPPORT.md`.
- Latest production readiness go/no-go from `docs/PRODUCTION_READINESS.md`.
- Open continuity exceptions, expiration dates, compensating controls, and
  accepting authority.

Do not commit private continuity records, topology diagrams, customer impact,
recovery locations, bucket names, vendor cases, internal hostnames, user lists,
or approval records to this public template.

## Production Gate

Before production launch or after a major recovery, the continuity gate passes
only when:

- The minimum viable platform is defined and owned.
- Required backups are current and restore proof is inside RPO/RTO.
- Break-glass access and recovery credentials are available to authorized
  operators.
- Dependency recovery order is current.
- Required live gates pass.
- Open exceptions are approved, time-bounded, and recorded privately.
- Post-recovery monitoring window is defined.

If any item is missing, record an exception in the private readiness evidence
and treat launch as no-go unless the accepting authority explicitly approves.
