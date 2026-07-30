# Operations Runbook

This runbook defines the minimum day-2 operating model for a production
deployment of the platform. It complements `docs/INSTALLATION.md`,
`docs/TROUBLESHOOTING.md`, `docs/BACKUP_RESTORE.md`,
`docs/BUSINESS_CONTINUITY.md`, `docs/SERVICE_CATALOG.md`,
`docs/CAPACITY_PLANNING.md`, `docs/COMPLIANCE_AUDIT.md`,
`docs/RELEASE_PROMOTION.md`, `docs/PRODUCTION_READINESS.md`,
`docs/ARCHITECTURE_DECISIONS.md`, and `docs/INCIDENT_RESPONSE.md`.

## Operating Principles

- Git is the source of truth for desired state.
- Argo CD applies platform and application state.
- CI builds, tests, scans, signs, and publishes artifacts; CI does not mutate
  the cluster directly.
- Operators use pull requests for production changes.
- Emergency changes are time-boxed, reviewed afterward, and reconciled back
  into Git.
- Production readiness is proven by live evidence, not by a successful
  repository-only validation run.

## Ownership

Every private deployment should identify owners for:

| Area | Required owner |
|---|---|
| Platform operations | RKE2, Cilium, MetalLB, Traefik, Argo CD |
| Source control | Forgejo, Gitea, or GitLab administration |
| CI | Woodpecker, runner pools, CI secrets, image publishing |
| Registry | Harbor projects, retention, vulnerability scanning, robot accounts |
| Database | CloudNativePG clusters, backups, restores, credentials |
| Storage | Longhorn or Rook/Ceph capacity, replication, degraded volumes |
| Observability | Grafana, Prometheus, Loki, alert routing, retention |
| Security | SOPS recipients, external secret stores, RBAC, audit review |
| Disaster recovery | Restore drill scheduling and acceptance evidence |

Keep owner names, escalation contacts, and internal on-call details in the
private deployment repository or ticketing system, not in this public template.
For Git review routing, copy `.github/CODEOWNERS.example` to
`.github/CODEOWNERS` in the private deployment repository, replace the
placeholder teams, and enable required reviewers through branch protection.

## Routine Checks

Run these checks on a schedule and after significant changes:

| Cadence | Check |
|---|---|
| Daily | `make platform-status` |
| Daily | Review Argo CD Applications for OutOfSync, Degraded, Unknown, or active operations |
| Daily | Check Longhorn degraded or faulted volumes |
| Daily | Confirm Velero BackupStorageLocation and latest backup status |
| Weekly | `make platform-app-health` |
| Weekly | Review Harbor storage usage, image retention, and vulnerability scan state |
| Weekly | Review Grafana dashboards and Prometheus alert health |
| Monthly | Review alert routing, noisy alerts, silences, and SLO/error budget state with `docs/ALERTING.md` |
| Monthly | Review Git repository access, CI secret access, and robot accounts |
| Monthly | Review P0/P1 service catalog owners, backup owners, dependencies, and runbooks with `docs/SERVICE_CATALOG.md` |
| Quarterly | Run the restore drill in `docs/BACKUP_RESTORE.md` |
| Quarterly | Run the continuity tabletop and minimum viable platform review in `docs/BUSINESS_CONTINUITY.md` |
| Quarterly | Review accepted architecture decisions and stale ADR review dates in `docs/ARCHITECTURE_DECISIONS.md` |

For production acceptance or post-upgrade proof, run:

```bash
PLATFORM_PROFILE=premium-3node make platform-production-check
```

The command includes `platform-image-inventory-verify`. Supply the exact
rendered-manifest summary and Cosign verification report produced for the
release through `PLATFORM_RENDERED_MANIFEST_SUMMARY` and
`COSIGN_VERIFICATION_REPORT`. When an upstream image is outside the private
registry, also supply the reviewed private exception file through
`PLATFORM_IMAGE_INVENTORY_EXCEPTIONS_FILE`. The image gate fails when a
rendered image cannot be resolved to an observed or explicitly reviewed
digest, a live image has no digest, a private-registry image is unsigned, or
an external image lacks a current admission exception.

Before approving launch, use `docs/PRODUCTION_READINESS.md` to collect the
go/no-go evidence package, open exceptions, launch decision, and post-launch
validation plan.

## Change Management

Normal production changes should follow this path:

1. Open a pull request in the private GitOps repository.
2. Confirm the change has an owner, reason, rollback plan, and maintenance
   window when service impact is possible.
3. Run repository validation.
4. Run profile validation for the selected deployment profile.
5. Merge after review.
6. Let Argo CD sync the change.
7. Run `make platform-status` and the relevant health gate.
8. Record the result in the change ticket or release evidence.

Use `make platform-app-health` for broad platform changes and
`make platform-ci-health` for Argo CD or Woodpecker-only changes.
Use `docs/RELEASE_PROMOTION.md` for dev, staging, production promotion gates,
rollback or roll-forward planning, hotfixes, freezes, and release evidence.

## Operator Command Bounds

First-party Python tools bound every child process. The defaults are sized for
their workload: short `kubectl`, GitHub CLI, Kyverno, and Cosign calls receive
short deadlines, render and validation children receive longer deadlines, and
Git/LFS migration receives two hours. Expiration fails the command and names
the stalled operation; Forge migration diagnostics redact credential-bearing
URLs.

Use a specific override only for a measured slow operation:

- `PLATFORM_KUBECTL_COMMAND_TIMEOUT_SECONDS`
- `FORGE_MIGRATION_COMMAND_TIMEOUT_SECONDS`
- `PLATFORM_VALIDATION_SCRIPT_TIMEOUT_SECONDS`
- `PLATFORM_RENDER_COMMAND_TIMEOUT_SECONDS`
- `PLATFORM_KYVERNO_COMMAND_TIMEOUT_SECONDS`
- `GITHUB_API_COMMAND_TIMEOUT_SECONDS`
- `PLATFORM_COSIGN_COMMAND_TIMEOUT_SECONDS`

`PLATFORM_SUBPROCESS_TIMEOUT_SECONDS` is the global fallback when no specific
override is set. Every value must be finite, positive, and no greater than
`86400` seconds. Treat a timeout as a failed operation and investigate the
child process; increasing a bound is not proof that the underlying operation
is healthy.

Captured child output is bounded independently of command duration. The shared
runner drains stdout and stderr concurrently, retains at most 32 MiB combined
by default, and terminates a child that crosses that ceiling. Set
`PLATFORM_SUBPROCESS_OUTPUT_MAX_BYTES` only for a measured command that needs a
larger diagnostic payload; the hard maximum is 256 MiB (`268435456` bytes).
Values must be whole, positive byte counts within that ceiling. Exceeding the
limit fails the operation while preserving only bounded output; it must not be
worked around by disabling capture controls.

First-party local file inputs are bounded before decoding, JSON/YAML parsing,
or hashing. The default maximum is 64 MiB. Set
`PLATFORM_FILE_INPUT_MAX_BYTES` only for a measured input that must be larger;
the hard maximum is 512 MiB (`536870912` bytes). Values must be whole, positive
byte counts within that ceiling. An oversized evidence file, migration plan,
inventory, configuration file, or rendered manifest fails the operation. Do
not bypass the bound; confirm the expected producer and payload size first.

Direct first-party HTTP clients use `PLATFORM_HTTP_TIMEOUT_SECONDS`, defaulting
to `30` seconds with a hard maximum of `300` seconds. API response bodies are
read only through the shared bounded reader. The default maximum is 16 MiB;
`PLATFORM_HTTP_RESPONSE_MAX_BYTES` may raise it to at most 64 MiB for a measured
large response. Non-numeric, non-positive, non-finite, fractional byte, or
over-ceiling values fail closed. GitHub CLI fallback output is checked against
the same byte limit before JSON parsing. An oversized response is an operation
failure, not a reason to disable the bound; confirm pagination and expected API
payload size before changing it.

## Controlled Pruning

Argo CD automatically reconciles creates and updates, but every Application
uses `Prune=confirm`. A resource removed from Git is therefore held for an
explicit, time-stamped deletion approval. `PruneLast=true` applies approved
deletions only after the other sync phases succeed,
`PrunePropagationPolicy=foreground` waits for dependants, and
`allowEmpty=false` prevents an empty render from deleting an entire
Application.

Run `make platform-app-health` after registration or a policy change. Its live
Application probe verifies that pruning, self-healing, empty-target protection,
confirmation, final-wave ordering, and foreground propagation are present on
the objects Argo CD is actually reconciling. Do not treat a static manifest
check alone as production proof.

The same health gate verifies that the singleton Forgejo deployment still uses
the non-overlapping `Recreate` strategy and a `minAvailable: 1`
PodDisruptionBudget. This protects its RWO volume during voluntary disruption;
it does not make Forgejo highly available during node loss or upgrades.

Prove the active-passive recovery path before production and at least
quarterly. This command intentionally cordons the current Forgejo node and
deletes only the managed singleton pod after checking that another Ready,
schedulable node exists and that the Deployment, PDB, service endpoint,
immutable image, PVC, encrypted PV, CSI key references, and Longhorn volume are
healthy:

```bash
PLATFORM_FORGEJO_RECOVERY_OPERATOR="${OPERATOR_ID:?set OPERATOR_ID}" \
PLATFORM_FORGEJO_RECOVERY_APPROVER="${INDEPENDENT_APPROVER_ID:?set INDEPENDENT_APPROVER_ID}" \
PLATFORM_FORGEJO_RECOVERY_CONFIRMATION=FAILOVER_FORGEJO_SINGLETON \
make platform-forgejo-recovery-drill
```

The drill waits for a different pod UID on a different node, requires
`/api/healthz` to return HTTP 200 through the same ClusterIP, verifies the same
persistent and runtime image identities, and fails if recovery exceeds
`PLATFORM_FORGEJO_RECOVERY_MAX_RTO_SECONDS`. A `finally` cleanup path uncordons
the source node; failed cleanup prevents passing evidence. It writes ignored
private evidence to `PLATFORM_FORGEJO_RECOVERY_EVIDENCE_FILE`. Routine health
checks never run this disruptive drill.

Before approving a prune:

1. Review the exact resources marked for deletion in the Argo CD diff.
2. Confirm the deletion is present in the approved pull request and change
   ticket.
3. Confirm current backups and restore evidence for every stateful resource.
4. Record a named approver who is different from the change author where the
   production control policy requires separation of duties.

Approve one Application at a time from an RKE2 server:

```bash
K=/var/lib/rancher/rke2/bin/kubectl
C=/etc/rancher/rke2/rke2.yaml
APP="${APP:?export APP with the Argo CD Application name}"
APPROVED_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

"$K" --kubeconfig "$C" -n argocd annotate application "$APP" \
  argocd.argoproj.io/deletion-approved="$APPROVED_AT" --overwrite
```

Wait for that Application to become `Synced` and `Healthy`, verify the
remaining resources, then remove the approval marker:

```bash
"$K" --kubeconfig "$C" -n argocd annotate application "$APP" \
  argocd.argoproj.io/deletion-approved-
```

Never pre-approve every Application, reuse an old approval as a standing
authorization, or approve pruning while the rendered profile is incomplete.
Record the diff, approval time, sync result, and post-change health in private
release evidence.

## Maintenance Windows

Use a maintenance window for:

- RKE2 version upgrades.
- Cilium, kube-proxy, CoreDNS, MetalLB, or Traefik changes.
- Longhorn engine, replica, disk, or storage class changes.
- Argo CD HA, Redis HA, or repo-server changes.
- Harbor, CloudNativePG, Forgejo, or Woodpecker database changes.
- CA, issuer, trust bundle, SOPS recipient, or external secret store changes.

Before the window:

- Confirm backups and the latest restore drill evidence.
- Confirm `make platform-production-check` passes or document known exceptions.
- Prepare rollback commits or chart version reverts.
- Pause nonessential automated syncs if the change could create cascading
  reconciliation.

After the window:

- Confirm Argo CD sync and health.
- Confirm GUI FQDNs and app VIP routes.
- Confirm storage, database, backup, and registry health.
- Resume paused syncs.
- Record elapsed time, issues, and follow-up actions.

## Upgrade Procedure

Use `docs/PLATFORM_SUPPORT.md` before upgrades to confirm support tier,
lifecycle status, compatibility gates, and any private exception or
deprecation record for the component being changed.

For platform component upgrades:

1. Review upstream release notes and breaking changes.
2. Update pinned chart versions, image tags, or digests in Git.
3. Run repository validation.
4. Render or validate the selected private profile.
5. Apply first in a lab or staging cluster when available.
6. Confirm backup and rollback evidence.
7. Merge during the maintenance window.
8. Watch Argo CD sync, workloads, PVCs, and ingress.
9. Run `make platform-production-check`.
10. Capture the evidence in the release record.

For RKE2 or node operating system upgrades, upgrade one node at a time and
prove etcd quorum, node readiness, Cilium readiness, Longhorn replica health,
and ingress VIP service before moving to the next node.

## Access Control

Use `docs/ACCESS_CONTROL.md` as the detailed access-control workflow. The
summary below is the minimum operating baseline.

Use least privilege:

- Give day-to-day users application or namespace-level access, not cluster-admin.
- Keep Argo CD admin access limited to platform operators.
- Use groups and projects in Argo CD, Forgejo, Harbor, and Grafana.
- Use robot accounts for automation and rotate them on a schedule.
- Store production secrets in SOPS, External Secrets, Sealed Secrets,
  Vault/OpenBao, or another approved private secret system.
- Do not share kubeconfigs, age private keys, CI tokens, Harbor robot tokens, or
  Argo CD admin passwords in chat or tickets.

Review access monthly and after personnel or vendor changes.

## Break-Glass Access

Break-glass access is for incidents only.

Minimum requirements:

- A named approver or incident commander.
- A time-boxed access window.
- A recorded reason.
- Commands and resources touched.
- Post-incident review.
- Follow-up Git change if the cluster was changed manually.

After break-glass use, rotate exposed credentials and reconcile any live drift
back to Git.

## Incident Response

Use `docs/INCIDENT_RESPONSE.md` as the detailed incident workflow. The summary
below is the minimum entry point.

Classify incidents by impact:

| Severity | Example |
|---|---|
| SEV1 | Kubernetes API unavailable, data loss risk, registry unavailable for production deployments |
| SEV2 | Argo CD or CI unavailable, degraded storage redundancy, backup target unavailable |
| SEV3 | Single app GUI route broken, one worker path degraded, noncritical alerting issue |

First response:

1. Stabilize the platform and protect data.
2. Stop risky automated changes if needed.
3. Run `make platform-status`.
4. Run the narrow health gate for the failing layer.
5. Use `docs/TROUBLESHOOTING.md` for the first failing evidence.
6. Record timeline, impact, commands, and recovery action.
7. Reconcile manual changes back to Git.

## Drift Management

manual cluster changes are temporary. If an operator patches a live resource:

- Record why it was necessary.
- Create the matching Git change or revert the live patch.
- Hard refresh Argo CD after the Git state is corrected.
- Run the relevant health gate.

Do not leave production relying on `kubectl patch`, manual Secret edits, or
untracked Helm changes.

## Credential Rotation

Rotate credentials after incidents, personnel changes, vendor changes, or at
the organization's normal interval.

High-value credentials include:

- SOPS age recipients and private keys.
- Argo CD admin and repository credentials.
- Forgejo admin, OAuth, and robot credentials.
- Woodpecker agents, OAuth, and database credentials.
- Harbor admin, robot, database, Redis, and object storage credentials.
- CloudNativePG backup object storage credentials.
- Velero object storage credentials.
- Grafana admin and database credentials.
- step-ca passwords and CA material when step-ca is enabled.

After rotation, rerun `make platform-app-secrets`, sync affected applications,
and prove readiness with `make platform-app-health`.

## Capacity and Retention

Track these before they become incidents:

- Node CPU, memory, disk, and inode usage.
- Longhorn capacity, replica health, snapshot count, and backup age.
- PostgreSQL storage, WAL growth, and backup retention.
- Harbor registry storage, retention policy, and project quotas.
- Loki and Prometheus retention windows and disk/object storage usage.
- Velero backup age and failed backup count.
- Git repository growth and large file policy.

Capacity increases should be done through Git and validated during a
maintenance window when they can affect storage or database behavior.
Use `docs/DATA_CLASSIFICATION.md` when retention, disposal, backup scope,
logging, registry pruning, or database/storage sizing changes can affect data
handling obligations.
Use `docs/CAPACITY_PLANNING.md` for the detailed capacity planning workflow:
baseline inventory, saturation signals, load tests, scale decisions, review
cadence, and private capacity evidence.
Use `docs/COMPLIANCE_AUDIT.md` to keep control domains, audit logging,
exceptions, evidence records, and review cadence consistent across private
deployment reviews.

## Production Evidence

Keep private evidence for:

- Latest `make platform-production-check`.
- Hash-bound rendered/live image inventory and OpenBao ceremony reports retained by the schema-v6
  production evidence packet.
- Latest `make platform-app-health`.
- Latest production readiness go/no-go record from `docs/PRODUCTION_READINESS.md`.
- Latest service catalog ownership and dependency review from `docs/SERVICE_CATALOG.md`.
- Latest architecture decision review from `docs/ARCHITECTURE_DECISIONS.md`.
- Latest restore drill.
- Latest continuity tabletop and minimum viable platform review from `docs/BUSINESS_CONTINUITY.md`.
- Latest access review.
- Latest capacity planning review from `docs/CAPACITY_PLANNING.md`.
- Latest compliance and audit evidence review from `docs/COMPLIANCE_AUDIT.md`.
- Latest release and environment promotion evidence from `docs/RELEASE_PROMOTION.md`.
- Latest sanitized GitHub governance evidence from
  `docs/REPOSITORY_GOVERNANCE.md` when GitHub publishes the release.
- Latest Alertmanager receiver and routing test from `docs/ALERTING.md`.
- Latest credential rotation.
- Latest maintenance window.
- Latest incident review when an incident occurred.

Evidence belongs in the private deployment repository, ticketing system, or
operations knowledge base. Do not commit private operational records to this
public template.
