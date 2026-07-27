# Production Readiness Checklist

This checklist defines a public-safe go/no-go model for private platform
deployments. It does not contain real owners, hostnames, IP addresses,
customer names, ticket links, screenshots, audit exports, or launch approvals.
Keep those records in the private deployment repository or release system.

Use this document before calling a cluster production-ready, before a major
platform release, and after a material incident or rebuild. Passing repository
validation alone is not production readiness; readiness requires live cluster
proof, restore proof, access proof, private evidence, and accepted exceptions.

## Principles

- Production readiness is proven by current evidence.
- Every readiness item must have an owner or accepting authority.
- A skipped gate is an exception, not a pass.
- Expired exceptions block launch.
- Private deployment records hold real evidence; this template defines the
  evidence model.
- The final decision should be reproducible from commands, pull requests,
  health gates, restore drills, and private review records.

## Readiness Scope

The readiness decision covers:

| Area | Required proof |
|---|---|
| Repository safety | Validation, secret scanning, pinned references, public-safe tracked files |
| RKE2 cluster | Node readiness, API VIP readiness, CNI and service path health |
| GitOps source | Argo CD Applications point at the intended private production source |
| Ingress | App VIP, HTTP-to-HTTPS redirect, GUI FQDNs, and backend endpoints work |
| Stateful data | PVCs, Longhorn or alternate storage, CloudNativePG, and app databases are ready |
| Backup and recovery | Off-cluster backups exist and restore drills pass |
| Business continuity | Minimum viable platform, dependency recovery order, failover/failback expectations, and continuity exercises are current through `docs/BUSINESS_CONTINUITY.md` |
| Service catalog | Production services have owners, backup owners, criticality, dependencies, SLO/SLA expectations, and recovery metadata through `docs/SERVICE_CATALOG.md` |
| Architecture decisions | Significant platform decisions are accepted, current, and reviewable through `docs/ARCHITECTURE_DECISIONS.md` |
| Platform apps | Argo CD, Forgejo, Woodpecker, Harbor, monitoring, Loki, Velero, cert-manager, trust-manager, and optional step-ca are healthy when required |
| Support and lifecycle | Supported OS, component versions, upgrade path, and exceptions are reviewed through `docs/PLATFORM_SUPPORT.md` |
| Access control | Human roles, robot accounts, branch protection, and break-glass flow are reviewed |
| Security and supply chain | SOPS or external secrets, policy examples, image/chart pinning, CI SHA pinning, and update review are in place |
| Admission controls | Kyverno managed-policy reports are reviewed; Enforce promotion has zero managed violations |
| Operations | Owners, maintenance windows, incident response, alerting, capacity, compliance evidence, and release promotion are current |

## Go/No-Go Checklist

Do not approve production until each statement is true or has a current,
accepted exception in `docs/COMPLIANCE_AUDIT.md`.

| Check | Required evidence |
|---|---|
| Repository validation passed | `python scripts/run_validation.py` or `make validate` |
| Secret scan passed | `make no-secrets` or `python scripts/validate_no_secrets.py` |
| Policy readiness reviewed | `make platform-policy-readiness`; Enforce mode requires zero managed violations |
| Profile validation passed | `PLATFORM_PROFILE=<PROFILE> make platform-profile-check` |
| Live production gate passed | `PLATFORM_PROFILE=<PROFILE> make platform-production-check` |
| Wildcard TLS deployed | `PLATFORM_WILDCARD_TLS_CERT_FILE=<CERT> PLATFORM_WILDCARD_TLS_KEY_FILE=<KEY> make platform-tls` |
| TLS boundary verified | `make platform-tls-verify` |
| Commit-bound acceptance retained | `PLATFORM_RELEASE_ID=<ID> PLATFORM_EVIDENCE_OPERATOR=<OPERATOR> PLATFORM_EVIDENCE_APPROVER=<APPROVER> make platform-production-evidence` |
| App health gate passed | `make platform-app-health` |
| RKE2 verification passed | `make rke2-verify` or production gate output |
| Platform status reviewed | `make platform-status` |
| Private Git source confirmed | Argo CD source URL matches the intended private repository |
| Temporary seed Git removed or explicitly accepted | No production Application relies on seed Git |
| Restore drill passed | Evidence from `docs/BACKUP_RESTORE.md` |
| Continuity exercise current | Evidence from `docs/BUSINESS_CONTINUITY.md` |
| Service catalog reviewed | Evidence from `docs/SERVICE_CATALOG.md` |
| Architecture decisions reviewed | Evidence from `docs/ARCHITECTURE_DECISIONS.md` |
| Access review passed | Evidence from `docs/ACCESS_CONTROL.md` |
| Support lifecycle reviewed | Evidence from `docs/PLATFORM_SUPPORT.md` and `docs/NODE_OS_SUPPORT.md` |
| Alert routing tested | Evidence from `docs/ALERTING.md` |
| Capacity review passed | Evidence from `docs/CAPACITY_PLANNING.md` |
| Compliance evidence reviewed | Evidence from `docs/COMPLIANCE_AUDIT.md` |
| Release promotion approved | Evidence from `docs/RELEASE_PROMOTION.md` |
| Threat model reviewed | Evidence from `docs/THREAT_MODEL.md` |
| Data classification reviewed | Evidence from `docs/DATA_CLASSIFICATION.md` |
| Incident process reviewed | Evidence from `docs/INCIDENT_RESPONSE.md` |
| Security policy reviewed | Evidence from `SECURITY.md` |
| Open exceptions reviewed | Owner, risk, compensating control, and expiration date |

## Required Live Gates

Run these against the target private deployment:

```bash
python scripts/run_validation.py
make no-secrets
PLATFORM_PROFILE=<PROFILE> make platform-profile-check
make rke2-verify
make platform-status
make platform-app-health
make platform-policy-readiness
PLATFORM_PROFILE=<PROFILE> make platform-production-check
```

After the production gate passes, retain a fresh private record that binds the
gate output to the exact Git revision and requires a distinct operator and
approver:

```bash
PLATFORM_RELEASE_ID=<APPROVED_CHANGE_ID> \
PLATFORM_EVIDENCE_OPERATOR=<OPERATOR_ID> \
PLATFORM_EVIDENCE_APPROVER=<INDEPENDENT_APPROVER_ID> \
make platform-production-evidence
```

The command writes a JSON record and hashed log below the ignored
`private/production-evidence/` directory. It never creates a passing record
when a gate fails.

When proving the exact private GitOps source, run the production gate with the
same repository URL used to register Argo CD Applications:

```bash
PLATFORM_REPO_URL=<PRIVATE_REPO_URL> make platform-production-check
```

Any temporary bypass such as skipping generated app secrets, storage classes,
HTTP redirects, Argo CD runtime, Longhorn runtime, Velero checks, certificate
checks, or seed Git source enforcement must be recorded as an exception.

## Component Acceptance Matrix

Use the table below as a private evidence prompt:

| Component | Acceptance proof |
|---|---|
| RKE2 and etcd | Nodes Ready, API reachable through the VIP, etcd quorum safe |
| Cilium, CoreDNS, and kube-proxy path | DNS, ClusterIP, and node-to-service probes pass |
| MetalLB, kube-vip, and ingress | API VIP and app VIP routes are reachable from expected clients |
| Traefik or alternate ingress | GUI hosts route to ready backends and redirect HTTP to HTTPS |
| Argo CD | Applications are Synced/Healthy, no stuck operation, source repository is production-safe |
| Forgejo, Gitea, or GitLab | Login, clone, push, backup, and repository storage checks pass |
| Woodpecker CI | Server and agents are Ready, image tags match, queue is healthy |
| Harbor | UI and registry API work, retention and vulnerability scanning are reviewed |
| CloudNativePG | Required clusters are Ready, backups and WAL archive are current |
| Longhorn or alternate storage | Required StorageClasses exist, volumes are healthy, restore test passes |
| Velero and object storage | BackupStorageLocation is Available, schedules are enabled, restore drill passes |
| Prometheus, Grafana, and Loki | API readiness, alerting, dashboards, retention, and log ingestion are reviewed |
| cert-manager and trust-manager | Certificates are Ready and Bundles are synced when required |
| step-ca | Health endpoint, CA storage, backup, and key ownership are reviewed when enabled |

## Exceptions and Deferrals

Record every exception with:

- Owner.
- Component.
- Requirement not met.
- Risk.
- Compensating control.
- Expiration date.
- Follow-up action.
- Approval authority.

Launch should stop when an exception affects data recovery, production source
trust, admin access, backup credentials, ingress availability, or a critical
security control unless the private governance process explicitly accepts the
risk.

## Launch Decision

Use this private decision record:

```text
Decision:
Date:
Deployment/profile:
Release or change reference:
Approver:
Operations owner:
Security owner:
Evidence package location:
Open exceptions:
Rollback or roll-forward plan:
Post-launch monitoring window:
Result:
```

The launch decision should reference, not duplicate, the detailed evidence from
the runbooks linked above.

## Post-Launch Validation

After launch or major release:

- Run `make platform-status`.
- Run `make platform-app-health`.
- Confirm Argo CD has no failed or stuck operations.
- Confirm GUI FQDNs and app VIP routes.
- Confirm backup schedules and latest backup status.
- Confirm alert routing and critical receivers.
- Confirm CI, registry, database, storage, logs, metrics, and certificates.
- Record issues, exceptions, and follow-up actions.

Keep a defined monitoring window after the release. If a rollback or
roll-forward is needed, follow `docs/RELEASE_PROMOTION.md` and
`docs/INCIDENT_RESPONSE.md`.

## Production Evidence

Keep private evidence for:

- Final go/no-go decision.
- Command outputs from required live gates.
- Restore drill and backup proof.
- Access-control review.
- Alert routing and SLO review.
- Capacity and saturation review.
- Compliance and audit evidence review.
- Release promotion approval.
- Threat model and data classification review.
- Open exceptions and expiration dates.
- Post-launch validation result.

Do not commit private readiness packets, screenshots, audit exports, customer
impact notes, internal hostnames, user lists, ticket links, or launch approvals
to this public template.
