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
- Local proof, state, inventory, governance, and score artifacts are written
  through an atomic same-directory replacement with owner-only file mode;
  interrupted writes must not replace the last complete record.
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
| Stateful data | PVCs use encrypted storage with recoverable externally escrowed keys; Longhorn or alternate storage, CloudNativePG, and app databases are ready |
| Backup and recovery | Off-cluster backups exist and restore drills pass |
| Business continuity | Minimum viable platform, dependency recovery order, failover/failback expectations, and continuity exercises are current through `docs/BUSINESS_CONTINUITY.md` |
| Service catalog | Production services have owners, backup owners, criticality, dependencies, SLO/SLA expectations, and recovery metadata through `docs/SERVICE_CATALOG.md` |
| Architecture decisions | Significant platform decisions are accepted, current, and reviewable through `docs/ARCHITECTURE_DECISIONS.md` |
| Platform apps | Argo CD, Forgejo, Woodpecker, Harbor, monitoring, Loki, Velero, cert-manager, trust-manager, and optional step-ca are healthy when required |
| Support and lifecycle | Supported OS, component versions, upgrade path, and exceptions are reviewed through `docs/PLATFORM_SUPPORT.md` |
| Access control | Human roles, robot accounts, branch protection, and break-glass flow are reviewed |
| Security and supply chain | SOPS or external secrets, image/chart pinning, CI SHA pinning, parser fuzzing, branch-coverage evidence, update review, release-time Cosign proof, and admission-time signature verification are in place |
| Admission controls | Three stable Kyverno CEL policies plus the stable image-signature policy are Ready, legacy policies are pruned, and Enforce promotion has zero managed violations plus a successful signed/invalid canary |
| Operations | Owners, maintenance windows, incident response, alerting, capacity, compliance evidence, and release promotion are current |

## Go/No-Go Checklist

Do not approve production until each statement is true or has a current,
accepted exception in `docs/COMPLIANCE_AUDIT.md`.

| Check | Required evidence |
|---|---|
| Repository validation passed | `python scripts/run_validation.py` or `make validate` |
| Migration parser robustness passed | The pinned ClusterFuzzLite workflow has no unresolved crashes, and `bash scripts/forge-coverage.sh` passes the 81.0% subprocess branch-coverage ratchet with retained JSON/XML evidence |
| Rendered Kubernetes schemas passed | `make rendered-schema-verify` and `make rendered-private-schema-verify`; the synthetic complete premium profile and exact private profile render without skipped applications, and built-in objects pass strict Kubeconform validation |
| Active admission policies compiled | `KYVERNO_BIN=<KYVERNO_1_18_1_BINARY> make policy-cel-verify`; compliant, violating, second-rule, and privileged-namespace fixtures produce the expected decisions, and the stable image policy compiles without registry access |
| Secret scan passed | `make no-secrets` or `python scripts/validate_no_secrets.py` |
| Policy enforcement accepted | `PLATFORM_POLICY_ENFORCEMENT=Enforce make platform-policy-readiness`; all three managed ValidatingPolicies must be Ready with `Deny`, both legacy ClusterPolicies must be absent, and managed violations must be zero |
| Image integrity enforcement accepted | `PLATFORM_IMAGE_INTEGRITY_MODE=Enforce PLATFORM_IMAGE_INTEGRITY_REQUIRED=true PLATFORM_IMAGE_INTEGRITY_CANARY_IMAGE=<SIGNED_DIGEST> make platform-policy-readiness`; the ImageValidatingPolicy must be Ready and fail closed, the signed digest must be admitted, and a derived unverifiable digest must be rejected by that policy |
| East-west isolation verified | `make platform-network-isolation-verify`; all premium policies exist, the trusted database path works, and the untrusted path is denied |
| Internal transport TLS verified | `make platform-internal-tls-verify`; managed trust is Ready, OpenBao/PostgreSQL/Valkey identities verify without insecure skips, Valkey clients use `rediss://`, and plaintext Valkey commands are rejected |
| OpenBao HA readiness verified | `make platform-openbao-verify`; at least three current and Ready replicas are initialized, unsealed, HA-enabled, and report one shared non-empty cluster identity |
| OpenBao custody and recovery accepted | `EVIDENCE=private/openbao-ceremony/<CEREMONY>.json make platform-openbao-ceremony-evidence-verify`; evidence binds the current OpenBao configuration and live cluster identity, independent 5-of-3-or-stronger custody, encrypted-at-creation recovery material, root-token revocation, audit/auth bootstrap, and a recovery test no older than 180 days |
| Observability delivery verified | `make platform-observability-verify`; Loki rejects anonymous requests, Alloy logs are queryable, retention is active, and Alertmanager configuration is valid. The production gate also sends a synthetic alert and requires a successful delivery metric |
| Supply-chain evidence verified | `COSIGN_IMAGES_FILE=<PRIVATE_INVENTORY> make supply-chain-verify`; scanners pass, the SPDX SBOM is non-empty, Scorecard meets threshold, and digest-bound Cosign verification succeeds |
| Exact image inventory reconciled | `PLATFORM_IMAGE_INVENTORY_EXCEPTIONS_FILE=<PRIVATE_EXCEPTIONS> make platform-image-inventory-verify`; every rendered and live runtime image resolves to one digest, private images are signed and admission-enforced, and any upstream admission gap has current independent approval plus hash-bound vulnerability evidence |
| Runtime capacity verified | `make platform-capacity-verify`; every node and Longhorn retain the configured filesystem, scheduler, and storage headroom, encrypted StorageClasses and CSI Secret references are valid, and every bound Longhorn volume is encrypted |
| Profile validation passed | `PLATFORM_PROFILE=<PROFILE> make platform-profile-check` |
| Live production gate passed | `PLATFORM_PROFILE=<PROFILE> make platform-production-check` |
| Wildcard TLS deployed | `PLATFORM_WILDCARD_TLS_CERT_FILE=<CERT> PLATFORM_WILDCARD_TLS_KEY_FILE=<KEY> make platform-tls` |
| TLS boundary verified | `make platform-tls-verify` |
| Commit-bound acceptance retained | `PLATFORM_RELEASE_ID=<ID> PLATFORM_EVIDENCE_OPERATOR=<OPERATOR> PLATFORM_EVIDENCE_APPROVER=<APPROVER> make platform-production-evidence` |
| Final production score passed | `make platform-production-score` verifies the signed checksum bundle and commit-matched live, governance, independent-approval, and release evidence, reports exactly 100/100, and exits zero |
| App health gate passed | `make platform-app-health` |
| Forgejo singleton recovery proven | Live Forgejo uses `Recreate` and a `minAvailable: 1` PodDisruptionBudget, and `make platform-forgejo-recovery-drill` produced current, independently approved, commit-bound cross-node evidence inside the accepted RTO without changing service, image, encrypted PVC/PV, or CSI key identity and with the source node restored schedulable |
| RKE2 verification passed | `make rke2-verify` or production gate output |
| Platform status reviewed | `make platform-status` |
| Private Git source confirmed | Argo CD source URL matches the intended private repository |
| Deletion control reviewed | `make platform-app-health` proves every live Application has self-healing plus approval-gated, last-wave foreground pruning with empty-app protection; any pending deletion has a named approver and evidence from `docs/OPERATIONS.md` |
| Temporary seed Git removed or explicitly accepted | No production Application relies on seed Git |
| Restore drill passed | Current schema-v2 evidence from `docs/BACKUP_RESTORE.md` proves timestamp-derived RPO/RTO, a separate failure domain, hash-bound component results, and data integrity |
| Continuity exercise current | Evidence from `docs/BUSINESS_CONTINUITY.md` includes successful DNS/VIP/TLS and consistency failover plus current-backup and data-reconciliation failback proof |
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
PLATFORM_POLICY_ENFORCEMENT=Enforce \
PLATFORM_IMAGE_INTEGRITY_MODE=Enforce \
PLATFORM_IMAGE_INTEGRITY_REQUIRED=true \
PLATFORM_IMAGE_INTEGRITY_CANARY_IMAGE=<PRIVATE_REGISTRY>/<IMAGE>@sha256:<64_HEX_CHARACTERS> \
make platform-policy-readiness
make platform-network-isolation-verify
make platform-internal-tls-verify
make platform-openbao-verify
make platform-observability-verify
make platform-capacity-verify
make rendered-schema-verify
make rendered-private-schema-verify
KYVERNO_BIN=<KYVERNO_1_18_1_BINARY> make policy-cel-verify
COSIGN_IMAGES_FILE=<PRIVATE_INVENTORY> make supply-chain-verify
PLATFORM_IMAGE_INVENTORY_EXCEPTIONS_FILE=<PRIVATE_EXCEPTIONS> \
make platform-image-inventory-verify
PLATFORM_FORGEJO_RECOVERY_OPERATOR=<OPERATOR_ID> \
PLATFORM_FORGEJO_RECOVERY_APPROVER=<INDEPENDENT_APPROVER_ID> \
PLATFORM_FORGEJO_RECOVERY_CONFIRMATION=FAILOVER_FORGEJO_SINGLETON \
make platform-forgejo-recovery-drill
PLATFORM_PROFILE=<PROFILE> make platform-production-check
```

After the production gate passes, retain a fresh private schema-v6 record that
binds every repository, profile, schema, supply-chain, cluster, security,
OpenBao readiness/custody, observability, capacity, application, and
data-protection gate to the exact Git revision and requires a distinct operator
and approver. The generator rejects a dirty or detached checkout and requires
`HEAD` to exactly match a fetched remote tracking ref; it stores the branch,
Git tree, remote name, and a non-secret hash of the remote URL. It also copies
the exact rendered/live image reconciliation and independently approved
OpenBao ceremony record into the private packet and binds both artifacts by
SHA-256. Earlier schema-v1/v2/v3/v4/v5 records remain historical evidence but
do not certify the current gate set:

```bash
PLATFORM_RELEASE_ID=<APPROVED_CHANGE_ID> \
PLATFORM_EVIDENCE_OPERATOR=<OPERATOR_ID> \
PLATFORM_EVIDENCE_APPROVER=<INDEPENDENT_APPROVER_ID> \
PLATFORM_OPENBAO_CEREMONY_EVIDENCE_FILE=private/openbao-ceremony/<CEREMONY>.json \
PLATFORM_PRODUCTION_EVIDENCE_EXPECTED_REF=seed/main \
make platform-production-evidence
```

The command writes a JSON record and hashed log below the ignored
`private/production-evidence/` directory. It never creates a passing record
when a gate fails. Run `git fetch <REMOTE>` first. If the current branch tracks
the reviewed deployment ref, the expected-ref setting can be omitted and the
upstream is used automatically. The runner uses `umask 077`, and the shared
artifact writer flushes and atomically replaces JSON evidence with mode `0600`.

## 100-Point Production Gate

Repository checks, live cluster acceptance, GitHub governance, independently
approved release execution, and signed release provenance are separate trust
boundaries. A production score of 100/100 requires all four retained evidence
records to identify the same 40-character commit. Partial evidence reports its
earned score but exits non-zero; it must not be used as launch approval.

Download the checksummed `*.github-governance.json`,
`*.github-release-approval.json`, and `*.github-release.json` files from the
immutable GitHub release, then run:

```bash
PLATFORM_PRODUCTION_EVIDENCE_FILE=private/production-evidence/<RELEASE>.json \
GITHUB_GOVERNANCE_EVIDENCE_FILE=private/release-evidence/<RELEASE>.github-governance.json \
GITHUB_RELEASE_EVIDENCE_FILE=private/release-evidence/<RELEASE>.github-release.json \
GITHUB_RELEASE_APPROVAL_EVIDENCE_FILE=private/release-evidence/<RELEASE>.github-release-approval.json \
GITHUB_RELEASE_CHECKSUMS_FILE=private/release-evidence/SHA256SUMS \
GITHUB_RELEASE_CHECKSUM_BUNDLE_FILE=private/release-evidence/SHA256SUMS.sigstore.json \
GITHUB_REPOSITORY=<OWNER>/<REPOSITORY> \
GITHUB_REF_NAME=<vMAJOR.MINOR.PATCH> \
PLATFORM_EXPECTED_COMMIT=<40_CHARACTER_RELEASE_COMMIT> \
PLATFORM_PROFILE=<PROFILE> \
PLATFORM_READINESS_SCORE_OUTPUT=private/production-evidence/<RELEASE>.score.json \
make platform-production-score
```

The gate first uses Cosign to verify the keyless `SHA256SUMS` signature against
the exact release workflow identity and GitHub Actions OIDC issuer. It then
assigns 80 points to fresh, independently approved live platform acceptance,
10 points to fresh GitHub governance, and 10 points to an independently approved
workflow run plus a verified annotated semantic-version tag and signed release
commit. It validates the underlying evidence schemas and SHA-256 bindings
rather than trusting a manually entered score. Only exactly 100/100 exits
successfully.

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
| Longhorn or alternate storage | Required encrypted StorageClasses and key references exist, every bound volume is encrypted and healthy, the key is escrowed outside the cluster, and an isolated unlock-and-restore test passes |
| Velero and object storage | BackupStorageLocation is Available, schedules are enabled, restore drill passes |
| Prometheus, Grafana, and Loki | API readiness, alerting, dashboards, retention, and log ingestion are reviewed |
| cert-manager and trust-manager | Certificates are Ready and Bundles are synced when required |
| Kyverno admission | CEL baselines and image-signature policy are Ready in Deny mode; signed admission and invalid-signature rejection canaries pass |
| step-ca | Health endpoint, CA storage, backup, and key ownership are reviewed when enabled |

The production `premium-3node` profile requires a maintained external,
off-cluster S3-compatible service. `premium-3node-lab` enables the archived
MinIO server for non-production testing and cannot satisfy this production
gate.

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
- Release-time Cosign inventory and admission-time signed/invalid canary proof.
- Threat model and data classification review.
- Open exceptions and expiration dates.
- Post-launch validation result.

Do not commit private readiness packets, screenshots, audit exports, customer
impact notes, internal hostnames, user lists, ticket links, or launch approvals
to this public template.
