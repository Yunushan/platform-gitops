# Threat Model

This threat model describes the public template and the private deployments
created from it. It is intentionally public-safe: keep real owners, domains,
IP addresses, incident details, exploit evidence, and compensating controls in
the private deployment repository or security system.

## Scope

In scope:

- Bootstrap automation, Ansible playbooks, Makefile targets, validation tools,
  and GitOps profile selection.
- RKE2 control plane, Cilium service networking, kube-vip or alternate API VIP,
  MetalLB, and Traefik or alternate ingress.
- Forgejo or alternate Git forge, Woodpecker or alternate CI, Argo CD,
  Harbor, CloudNativePG, Longhorn or alternate storage, monitoring, Loki,
  Velero, cert-manager, trust-manager, and optional step-ca.
- Repository governance: pull requests, CODEOWNERS, branch protection,
  required reviews, CI validation, and secret/privacy scanning.
- Secret handling patterns such as SOPS with age, External Secrets, Sealed
  Secrets, Vault/OpenBao, and ignored local files.

Out of scope:

- Vulnerabilities in upstream projects that do not depend on this template's
  configuration.
- Organization-specific firewall rules, identity providers, network segments,
  and production incident records.
- Application workloads deployed on top of the platform unless this template
  provides their scaffolding or policy.

## Assumptions

- The public repository does not contain real private deployment data.
- Private deployments use a private GitOps repository as the Argo CD source of
  truth after first bootstrap.
- Production changes flow through pull requests, required reviews, validation,
  and Argo CD sync.
- Operators keep SOPS age private keys, kubeconfigs, SSH keys, robot tokens,
  and backup credentials outside Git.
- The 3-node profile tolerates one node failure but still requires off-cluster
  backups and restore drill evidence.

## Assets

Protect these assets first:

| Asset | Why it matters |
|---|---|
| Kubernetes API and etcd | Cluster authority and desired live state |
| GitOps repository | Source of truth for platform configuration |
| Argo CD credentials and projects | Deployment authority into the cluster |
| Forgejo repositories and admin users | Source code, pull requests, and audit trail |
| Woodpecker secrets and agents | Build credentials and image publishing authority |
| Harbor projects and robot accounts | Artifact integrity and release availability |
| CloudNativePG data and backups | Persistent platform application state |
| Longhorn or alternate storage volumes | Stateful workload durability |
| Velero and object storage credentials | Disaster recovery authority |
| cert-manager, trust-manager, and step-ca material | TLS identity and trust distribution |
| SOPS age recipients and private keys | Secret confidentiality |
| Observability data | Incident evidence and sensitive operational metadata |

## Trust Boundaries

Review these boundaries before production use and whenever topology changes:

```text
operator workstation
  -> Git hosting and pull request review
  -> CI runners and artifact registry
  -> private GitOps repository
  -> Argo CD repo-server and application controller
  -> Kubernetes API
  -> cluster networking, ingress VIPs, and platform services
  -> storage, databases, backup targets, and observability stores
```

Key boundary checks:

- Workstations to Git hosting: require MFA, least privilege, signed or reviewed
  changes when the organization mandates them, and no shared credentials.
- Git hosting to CI: protect branch rules, runner registration secrets, and
  repository tokens.
- CI to registry: use scoped robot accounts, artifact scanning, and signature
  or attestation policy when enabled.
- GitOps repository to Argo CD: use a private repository, scoped deploy keys or
  tokens, and Argo CD projects.
- Argo CD to Kubernetes API: limit destination namespaces and cluster-scoped
  privileges to the components that require them.
- Ingress VIPs to platform services: confirm TLS, host routing, local endpoint
  coverage, and firewall exposure.
- Cluster to backups/object storage: protect credentials and prove restore,
  not only backup creation.

## Threat Scenarios

| Threat | Example | Primary controls |
|---|---|---|
| Secret leakage | Private key, kubeconfig, token, internal hostname, or private IP committed to a public repo | `.gitignore`, ignored local files, SOPS, `make no-secrets`, pull request public-safety checks |
| Unauthorized production change | A risky manifest or chart value is merged without owner review | Pull requests, `.github/CODEOWNERS.example` copied to private CODEOWNERS, branch protection, required reviews |
| Supply-chain compromise | Unreviewed chart, image, or CI Action change reaches production | Pinned chart versions, curated image pinning, CI SHA pinning, Renovate dashboard approval, staged Cosign/Kyverno verification, and signed/invalid admission canary proof |
| CI credential misuse | A compromised CI job pushes images or edits desired state | Scoped robot accounts, protected branches, isolated runners, secret rotation, no direct cluster deploy from CI |
| Argo CD over-privilege | One application can mutate unrelated namespaces or cluster resources | A namespace-only AppProject for ordinary services, a separate reviewed operator project, explicit destinations, drift review |
| Ingress or VIP exposure | Wrong host routes to a sensitive service or bypasses TLS expectations | Traefik/ingress validation, app VIP checks, HTTP to HTTPS redirect checks, DNS review |
| Storage or database loss | PVC, Longhorn volume, or PostgreSQL data is deleted or corrupted | Backup policies, restore drills, storage class review, maintenance windows |
| Backup target compromise | Backup credentials allow destructive or exfiltration access | Scoped object storage credentials, retention policy, credential rotation, restore evidence |
| PKI or trust compromise | Internal CA, issuer, trust bundle, or TLS private key is mishandled | cert-manager review, trust-manager bundle review, optional step-ca backup/key controls, secret scanning |
| Observability data leak | Logs or metrics expose credentials, usernames, or topology details | Loki/Grafana access control, retention review, log redaction, private incident records |
| Service-network failure | ClusterIP, DNS, or VIP path breaks deployments and health checks | Health gates, service-path repair runbooks, Cilium/kube-proxy/CoreDNS checks, production evidence |
| Capacity exhaustion | Nodes, databases, storage, CI, registry, or observability stores saturate before operators scale them | Capacity planning, saturation alerts, load tests, retention review, private evidence from `docs/CAPACITY_PLANNING.md` |
| Audit evidence gap | A production change, access grant, restore, exception, or incident cannot be traced later | Pull request history, audit logs, private evidence records, and `docs/COMPLIANCE_AUDIT.md` |
| Unsafe promotion | A change skips staging, rollback review, or production health gates | Protected branches, required reviews, release evidence, and `docs/RELEASE_PROMOTION.md` |

## High-Risk Changes

Require named owner approval and rollback evidence for changes to:

- Argo CD projects, repository credentials, RBAC, app-of-apps, or cluster
  destinations.
- CI runner images, runner privileges, secret scopes, registry publishing, or
  pipeline templates.
- Harbor robot accounts, retention policy, vulnerability scanning, or project
  permissions.
- CloudNativePG clusters, backup credentials, storage classes, and restore
  behavior.
- Longhorn disks, replica counts, storage classes, recurring jobs, and volume
  salvage behavior.
- cert-manager issuers, trust-manager bundles, step-ca material, and TLS
  secrets.
- MetalLB pools, kube-vip settings, ingress controllers, firewall exposure, and
  DNS mappings.
- SOPS recipients, external secret store references, SealedSecret keys, or
  Vault/OpenBao paths.
- Validation scripts, secret scanners, CI workflows, and policy examples.

Record significant accepted changes with `docs/ARCHITECTURE_DECISIONS.md` so
future threat reviews can trace context, options, consequences, validation, and
rollback or exit plans.

## Evidence

Private deployment owners should keep current evidence for:

- Latest `python scripts/run_validation.py`.
- Latest `make no-secrets`.
- Latest `PLATFORM_PROFILE=<PROFILE> make platform-production-check`.
- Latest restore drill from `docs/BACKUP_RESTORE.md`.
- Latest data classification and retention review from
  `docs/DATA_CLASSIFICATION.md`.
- Latest access review and CODEOWNERS/branch protection review.
- Latest access-control review from `docs/ACCESS_CONTROL.md`.
- Latest alert routing and SLO review from `docs/ALERTING.md`.
- Latest incident response review from `docs/INCIDENT_RESPONSE.md`.
- Latest capacity planning review from `docs/CAPACITY_PLANNING.md`.
- Latest compliance and audit evidence review from `docs/COMPLIANCE_AUDIT.md`.
- Latest release and environment promotion review from `docs/RELEASE_PROMOTION.md`.
- Latest credential rotation and incident review when applicable.
- Latest dependency, chart, image, and CI reference update review.

Do not commit private evidence to this public template.

## Review Cadence

Review the threat model:

- Before production launch.
- Before enabling a new component or alternate profile.
- After major RKE2, Cilium, Argo CD, Forgejo, Woodpecker, Harbor,
  CloudNativePG, Longhorn, Traefik, cert-manager, or storage changes.
- After an incident, secret exposure, failed restore drill, or failed
  production health gate.
- At least quarterly with the operations, security, storage, database,
  observability, and CI owners.
