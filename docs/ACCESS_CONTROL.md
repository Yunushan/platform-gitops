# Access Control Runbook

This runbook defines a public-safe access control model for private platform
deployments. Do not commit real users, groups, identity provider names,
domains, IP addresses, tokens, kubeconfigs, SSH keys, approval records, or
access-review evidence to this public template.

## Principles

- Use least privilege for humans, robots, CI, and service accounts.
- Prefer named users and groups over shared accounts.
- Require MFA for Git hosting, identity providers, cloud/object storage, and
  administrative consoles.
- Keep day-to-day access separate from break-glass access.
- Use short-lived or scoped credentials wherever the component supports them.
- Review access on a schedule and after personnel, vendor, or ownership
  changes.
- Map privileged access and robot accounts to service catalog ownership in
  `docs/SERVICE_CATALOG.md`.
- Record approvals, exceptions, and evidence in the private deployment
  repository or governance system.

## Access Domains

| Domain | Examples | Minimum control |
|---|---|---|
| Git hosting | Forgejo, Gitea, GitLab, GitHub mirror | MFA, protected branches, CODEOWNERS, least-privilege repository roles |
| CI | Woodpecker, GitLab Runner, Gitea Actions, runner pools | Scoped secrets, protected branches, controlled runner registration |
| CD | Argo CD UI, API, projects, repository credentials | Project scoping, limited admins, private repository credentials |
| Kubernetes | RKE2 API, kubectl, service accounts, ClusterRoles | RBAC review, no routine cluster-admin, audited kubeconfig use |
| Registry | Harbor projects, robot accounts, vulnerability reports | Project roles, scoped robot accounts, retention and scan permissions |
| Database | CloudNativePG clusters, database users, backup credentials | Application-scoped database roles, backup credential protection |
| Storage and backup | Longhorn, Velero, object storage, snapshots | Restricted backup/restore authority, object storage least privilege |
| Observability | Grafana, Prometheus, Loki, Alertmanager | Viewer/editor/admin separation, private receiver credentials |
| PKI and trust | cert-manager, trust-manager, step-ca, CA material | Restricted issuer and CA access, key backup and rotation controls |
| Operator workstations | SSH keys, kubeconfigs, age keys, Git credentials | Encrypted storage, MFA, no shared workstations for admin actions |

## Human Access

Private deployments should define:

- Platform operators.
- Security operators.
- Source-control administrators.
- CI administrators.
- Registry administrators.
- Database administrators.
- Storage and backup administrators.
- Observability administrators.
- Read-only auditors.
- Emergency break-glass users.

Each role should have:

```text
Role:
Owner:
Allowed systems:
Allowed actions:
Approval source:
Review cadence:
Removal trigger:
Break-glass allowed:
```

Avoid granting broad platform access just because a user needs one application
or one namespace.

## Kubernetes RBAC

Minimum expectations:

- Day-to-day users should not receive `cluster-admin`.
- Namespace owners should receive namespace-scoped Roles where possible.
- ClusterRoleBindings should be rare, reviewed, and tied to platform
  components or named operator groups.
- ServiceAccount token automounting should be disabled when a workload does not
  need the Kubernetes API.
- Generated kubeconfigs should be treated as restricted credentials.
- Etcd snapshots and Velero backups should be treated as containing Kubernetes
  Secrets even when the target workload seems low-risk.

Review before production:

```bash
kubectl get clusterrolebindings
kubectl get rolebindings -A
kubectl get serviceaccounts -A
```

Store review results privately after redacting any sensitive names.

## Argo CD Access

Use Argo CD projects to limit:

- Source repositories.
- Destination clusters.
- Destination namespaces.
- Cluster-scoped resources.

The supplied GitOps tree separates these powers. `platform` is reserved for
operators that install reviewed CRDs, webhooks, cluster RBAC, ingress/storage
classes, or other cluster-scoped resources. `platform-services` contains
Forgejo, Woodpecker, Harbor, Keycloak, MinIO, platform PostgreSQL, platform
Valkey, and step-ca; it may target only their explicit namespaces and may create
only Namespace as a cluster-scoped kind. Moving an application between these
projects is a privileged architecture change and must be reviewed against its
fully rendered manifests.
- Sync windows when required by change control.

Keep the built-in admin account for bootstrap and emergency access only when
possible. Day-to-day users should authenticate through the private identity
provider or Git hosting integration selected by the deployment owner.

Repository credentials are deployment authority. Store them in Kubernetes
Secrets, SOPS, External Secrets, Sealed Secrets, Vault/OpenBao, or another
approved private secret system.

## Git and Branch Protection

Private deployment repositories should enable:

- Protected main or production branches.
- Required pull requests.
- Required reviews from the private `.github/CODEOWNERS` file.
- Required validation checks.
- Required no-secrets or equivalent secret scanning.
- Dismiss stale approvals after sensitive changes when supported.
- Restricted force pushes and branch deletion.

Use `.github/CODEOWNERS.example` as the public-safe starter, then replace
placeholder teams in the private repository.

## CI and Robot Accounts

Robot accounts should be:

- Named by purpose.
- Scoped to the smallest repository, registry project, namespace, or object
  storage path.
- Stored only in approved secret systems.
- Rotated on schedule and after exposure.
- Removed when a pipeline or integration is retired.

CI should build, test, scan, sign, and publish artifacts. CI should not hold
routine cluster-admin credentials or directly mutate production unless an
explicit private deployment policy allows a narrow exception.

## Break-Glass Access

Break-glass access is temporary incident access.

Minimum controls:

- Named incident commander or approver.
- Start and end time.
- Reason and affected systems.
- Commands or actions performed.
- Credential rotation when exposure is possible.
- Post-incident review.
- Git reconciliation for manual cluster changes.

Use `docs/INCIDENT_RESPONSE.md` for the incident workflow and
`docs/OPERATIONS.md` for break-glass operating expectations.

## Access Review

Review access:

- Before production launch.
- Monthly for high-value admin and robot access.
- Quarterly for all platform roles.
- After personnel, vendor, ownership, or incident changes.
- After secret exposure, failed audit, failed restore drill, or failed
  production health gate.

Minimum review questions:

- Does every admin have a current business need?
- Are there shared accounts that should become named users or scoped robots?
- Are any users over-privileged for their current role?
- Are repository, CI, registry, database, backup, and Grafana admin users still
  current?
- Are service accounts and ClusterRoleBindings still required?
- Are SOPS age recipients and external secret-store policies current?
- Were disabled users removed from Git, CI, Argo CD, Harbor, Grafana,
  Kubernetes, object storage, and identity providers?

## Removal and Rotation

When a person, vendor, or integration loses access:

1. Remove Git hosting and repository permissions.
2. Remove CI, runner, and secret access.
3. Remove Argo CD, Kubernetes, registry, Grafana, database, backup, and object
   storage access.
4. Rotate credentials that may have been copied, shared, or exposed.
5. Update SOPS recipients or external secret policies.
6. Confirm Argo CD does not reapply stale credentials from Git.
7. Record completion in the private access-review evidence.

## Production Evidence

Keep private evidence for:

- Current role-to-system access matrix.
- Current Argo CD project and admin review.
- Current Kubernetes RBAC review.
- Current Git branch protection and CODEOWNERS review.
- Current robot account and CI secret review.
- Current registry, database, storage, backup, PKI, and observability access
  review.
- Latest break-glass use and post-incident follow-up.
- Latest credential rotation and removed-user review.

Do not commit private access matrices, screenshots, audit exports, identity
provider group names, or access-review records to this public template.
