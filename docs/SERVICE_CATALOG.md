# Service Catalog and Ownership

This document defines a public-safe service catalog model for private platform
deployments. Keep real team names, escalation paths, on-call schedules, service
IDs, customer names, internal URLs, ticket links, cost centers, and business
impact statements in the private deployment repository or service-management
system.

Use this catalog model with `docs/OPERATIONS.md`,
`docs/PRODUCTION_READINESS.md`, `docs/BUSINESS_CONTINUITY.md`,
`docs/ALERTING.md`, `docs/ACCESS_CONTROL.md`, `docs/DATA_CLASSIFICATION.md`,
`docs/CAPACITY_PLANNING.md`, `docs/COMPLIANCE_AUDIT.md`, and
`docs/RELEASE_PROMOTION.md`.

## Catalog Principles

- Every production service needs an owner, backup owner, criticality, support
  tier, data classification, and recovery expectation.
- Every paging alert needs a service owner and a runbook.
- Every service with state needs backup, restore, retention, and disposal
  expectations.
- Every service exposed through ingress needs an approved FQDN, certificate
  owner, and access model.
- Missing ownership, stale catalog entries, or unclear dependency maps are
  production readiness exceptions.

## Required Fields

Private deployments should maintain a catalog entry for each production service
with:

| Field | Purpose |
|---|---|
| Service name | Human-readable service name |
| Component | Argo CD Application, namespace, Helm chart, or external dependency |
| Criticality | P0, P1, P2, or P3 based on recovery priority |
| Owner | Accountable team or person in the private system |
| Backup owner | Secondary owner for absence or incident coverage |
| Support tier | Enterprise validated, compatible, lab, deprecated, or private exception |
| User entry points | FQDNs, API endpoints, CLI paths, or internal-only status |
| Dependencies | Upstream and downstream services required for operation |
| Data classification | Public template, confidential operational, regulated, or restricted |
| SLO/SLA target | Private service objective or accepted best-effort status |
| RPO/RTO target | Recovery point and recovery time expectation |
| Backup and restore | Backup source, restore proof, and drill cadence |
| Access model | Human, robot, admin, break-glass, and audit expectations |
| Observability | Dashboards, alerts, logs, traces, and runbook links |
| Capacity signals | Saturation indicators, quotas, retention, and scale triggers |
| Release model | Promotion gate, rollback or roll-forward model, freeze sensitivity |
| Continuity role | Minimum viable platform priority and recovery order |
| Review cadence | Monthly, quarterly, release-based, or exception-based review |

Do not publish real catalog values in this template.

## Platform Service Matrix

Use this matrix as a starter for private catalog records:

| Service | Default criticality | Catalog focus |
|---|---|---|
| RKE2 API and etcd | P0 | API VIP, etcd quorum, snapshots, kubeconfig access, node ownership |
| Cilium, CoreDNS, and kube-proxy path | P0 | Pod networking, DNS, ClusterIP, firewalld/sysctl prerequisites |
| kube-vip and MetalLB | P0/P1 | API VIP, app VIP, L2/ARP ownership, pool ownership |
| Traefik or alternate ingress | P1 | FQDN routing, TLS, redirect policy, backend health |
| Argo CD | P0 | GitOps source, admin access, repo-server, app controller, sync policy |
| Forgejo, Gitea, or GitLab | P0/P1 | Repository data, users, SSH keys, OAuth apps, backup and clone tests |
| Woodpecker CI or selected runner | P1 | OAuth, agents, queues, secrets, build logs, runner trust |
| Harbor | P1 | Registry artifacts, robot accounts, retention, vulnerability scanning |
| CloudNativePG | P1 | PostgreSQL clusters, WAL archive, backups, failover, restore proof |
| Longhorn or alternate storage | P0/P1 | Storage classes, replicas, disk pressure, volume backup and restore |
| Velero and object storage | P0/P1 | Backup schedules, BackupStorageLocation, restore drills |
| Prometheus, Grafana, and Loki | P1/P2 | SLOs, dashboards, alerting, log retention, incident evidence |
| cert-manager and trust-manager | P1 | Certificates, issuers, Bundles, trust distribution |
| step-ca | P1 | CA health, key ownership, backup, trust bootstrap |

## Dependency Map

Private catalog entries should describe dependencies in both directions:

- What this service needs to start.
- What needs this service to start.
- What data must be restored before this service is useful.
- Which ingress, DNS, certificate, identity, storage, database, or object
  storage dependency can block it.
- Which services can run degraded while this service recovers.

Use `docs/BUSINESS_CONTINUITY.md` to turn the dependency map into a recovery
order and continuity exercise.

## Ownership Review

Review service ownership:

- Monthly for P0 and P1 services.
- Quarterly for P2 and P3 services.
- Before production launch.
- Before major upgrades, component swaps, or profile changes.
- After incidents, personnel changes, vendor changes, failed restore drills,
  failed alert routing tests, or failed access reviews.

Each review should record owner, backup owner, stale entries, missing runbooks,
open exceptions, and next review date in the private catalog.

## Production Acceptance

Before a service is accepted as production-ready:

- Catalog entry is complete and owner-approved.
- Data classification and access model are reviewed.
- Backup, restore, and continuity expectations are defined when state exists.
- Alerting and dashboard links exist or a documented exception is approved.
- Capacity and retention signals are defined.
- Release and rollback or roll-forward model is defined.
- Support tier and lifecycle status are reviewed.
- Dependencies are mapped and tested through the relevant live gates.

## Evidence

Private deployments should keep evidence for:

- Current service catalog export or record set.
- Owner and backup owner review.
- Criticality and dependency map review.
- SLO/SLA or best-effort decision.
- RPO/RTO and continuity review.
- Access and robot-account review.
- Alert/dashboard/runbook review.
- Backup and restore proof for stateful services.
- Support lifecycle and deprecation review.
- Open exceptions, expiration dates, and accepting authority.

Do not commit private service catalogs, owner names, internal service IDs,
escalation paths, on-call schedules, customer impact, FQDN inventories, cost
centers, or approval records to this public template.
