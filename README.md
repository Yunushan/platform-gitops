<h1 align="center">Platform GitOps Workspace</h1>

<p align="center">
  <strong>Zero-subscription, enterprise-style CI/CD and GitOps platform workspace for 3-node private Kubernetes deployments.</strong>
</p>

<p align="center">
  <img alt="build" src="https://img.shields.io/badge/build-ready-brightgreen">
  <img alt="release" src="https://img.shields.io/badge/release-v0.1.0-blue">
  <img alt="license" src="https://img.shields.io/badge/license-0BSD-blue">
  <img alt="default" src="https://img.shields.io/badge/default-RKE2%203%20server%20nodes-orange">
  <img alt="git" src="https://img.shields.io/badge/git-Forgejo-purple">
  <img alt="ci" src="https://img.shields.io/badge/ci-Woodpecker%20CI-green">
  <img alt="cd" src="https://img.shields.io/badge/cd-Argo%20CD%20HA-red">
  <img alt="registry" src="https://img.shields.io/badge/registry-Harbor-blue">
  <img alt="database" src="https://img.shields.io/badge/database-CloudNativePG-informational">
  <img alt="storage" src="https://img.shields.io/badge/storage-Longhorn%20%7C%20Rook%2FCeph-lightgrey">
  <img alt="secrets" src="https://img.shields.io/badge/secrets-not%20included-success">
</p>

<p align="center">
  <a href="docs/QUICK_START.md">Quick Start</a> •
  <a href="docs/ARCHITECTURE.md">Architecture</a> •
  <a href="docs/ARCHITECTURE_DECISIONS.md">ADRs</a> •
  <a href="docs/BACKUP_RESTORE.md">Backup and Restore</a> •
  <a href="docs/BUSINESS_CONTINUITY.md">Business Continuity</a> •
  <a href="docs/OPERATIONS.md">Operations</a> •
  <a href="docs/PRODUCTION_READINESS.md">Production Readiness</a> •
  <a href="docs/PLATFORM_SUPPORT.md">Platform Support</a> •
  <a href="docs/SERVICE_CATALOG.md">Service Catalog</a> •
  <a href="docs/CAPACITY_PLANNING.md">Capacity</a> •
  <a href="docs/COMPLIANCE_AUDIT.md">Audit Evidence</a> •
  <a href="docs/RELEASE_PROMOTION.md">Promotion</a> •
  <a href="docs/ALERTING.md">Alerting</a> •
  <a href="docs/INSTALLATION.md">Launch</a> •
  <a href="docs/PREMIUM_3NODE.md">Premium 3-Node</a> •
  <a href="docs/PRIVATE_DEPLOYMENT.md">Private Deployment</a> •
  <a href="docs/COMPONENT_SWITCHING.md">Change Components</a> •
  <a href="docs/FORGE_MIGRATION.md">Forge Migration</a> •
  <a href="docs/FORGE_CUTOVER.md">Forge Cutover</a> •
  <a href="docs/SECRETS_AND_PRIVACY.md">Secrets & Privacy</a> •
  <a href="SECURITY.md">Security</a> •
  <a href="docs/USER_GUIDE.md">User Guide</a> •
  <a href="docs/RELEASE_GUIDE.md">Release Guide</a> •
  <a href="LICENSE">License</a>
</p>

---

A private-first platform workspace for building a **3-node RKE2 Kubernetes CI/CD and GitOps environment**. The default platform is:

```text
Rocky Linux 10, 3 server nodes
RKE2 Kubernetes with Cilium CNI
Forgejo
Woodpecker CI with HA server replicas and 3 Kubernetes agents
Argo CD HA
Keycloak SSO
Harbor
CloudNativePG PostgreSQL
Longhorn storage, with Rook/Ceph as an alternative
MetalLB + Traefik, with ingress-nginx as an alternative
Prometheus + Grafana + Loki
Velero + off-cluster backups
cert-manager + trust-manager, optional step-ca internal CA
SOPS + age, External Secrets Operator, and OpenBao-ready secret management
Kyverno audit baseline and policy examples
Tetragon eBPF runtime observability
Cosign + Renovate supply-chain helpers, plus Trivy, Gitleaks, and local Semgrep scans
Virtual IP / VIP for highly available access
```

The project is intentionally modular. You can switch from **Forgejo** to **Gitea** or **GitLab CE**, from **Longhorn** to **Rook/Ceph**, and from **Traefik** to **ingress-nginx** by changing profile files and GitOps paths rather than redesigning the repository.

For a hardened private deployment, use the **premium 3-node profile**:

```text
profiles/premium-3node.yaml
gitops/clusters/rke2-main/premium-3node
```

Bootstrap it with `PLATFORM_PROFILE=premium-3node PLATFORM_APPLY_GITOPS=true
PLATFORM_REPO_URL=<PRIVATE_REPO_URL> make platform-argocd` after rendering or
skipping incomplete private values as documented in `docs/PRIVATE_DEPLOYMENT.md`.

## Privacy and secret-safety promise

This repository is designed to be pushed publicly or privately without leaking sensitive information.

```text
No real company data
No real user data
No real IP addresses
No real domain names
No real tokens
No real passwords
No private keys
No kubeconfigs
No SSH keys
```

All local values are placeholders. Real values belong only in ignored local files, external secret managers, SOPS-encrypted files, or sealed-secret workflows.

For a company deployment, keep this repository as the public 0BSD template and
use a separate private deployment repository as the Argo CD source. See
[`docs/PRIVATE_DEPLOYMENT.md`](docs/PRIVATE_DEPLOYMENT.md).

Run this before pushing:

```bash
make validate
make no-secrets
```

`make validate` is a repository-only gate. It needs Python 3 and Bash, and it
checks structure, renderers, privacy scanners, shell syntax, and the production
contract. It does not contact a live cluster. If `make` is not installed, run
the same validation suite with:

```bash
python scripts/run_validation.py
```

Run this before calling a deployed cluster production-ready:

```bash
PLATFORM_PROFILE=premium-3node make platform-profile-check
make platform-production-check
```

The production gate is fail-closed: it requires external etcd, Velero,
CloudNativePG, and Longhorn backups inside the configured RPO plus a recent,
independently approved restore record referenced by
`PLATFORM_RESTORE_EVIDENCE_FILE`. Start from
[`examples/restore-evidence.example.json`](examples/restore-evidence.example.json)
and keep the completed record under ignored `private/` storage.

Use [`docs/PRODUCTION_READINESS.md`](docs/PRODUCTION_READINESS.md) as the
go/no-go checklist for live gates, evidence, exceptions, launch decision, and
post-launch validation.
Before production use, also complete the restore drill in
[`docs/BACKUP_RESTORE.md`](docs/BACKUP_RESTORE.md) and keep the evidence in
your private deployment record.
Use [`docs/BUSINESS_CONTINUITY.md`](docs/BUSINESS_CONTINUITY.md) to define the
minimum viable platform, dependency recovery order, RPO/RTO continuity model,
failover/failback expectations, and continuity exercise evidence.
Use [`docs/SERVICE_CATALOG.md`](docs/SERVICE_CATALOG.md) to keep service
ownership, criticality, dependencies, SLO/SLA expectations, access model, and
recovery expectations reviewable in private records.
Use [`docs/ARCHITECTURE_DECISIONS.md`](docs/ARCHITECTURE_DECISIONS.md) to
record significant platform choices, alternatives, consequences, validation,
and rollback or exit plans.
Use [`docs/OPERATIONS.md`](docs/OPERATIONS.md) for day-2 ownership, change
management, incident, access, rotation, and maintenance-window practices.
Use [`docs/INCIDENT_RESPONSE.md`](docs/INCIDENT_RESPONSE.md) for severity
declaration, incident roles, communications, recovery validation, and
post-incident review.
Use [`docs/ACCESS_CONTROL.md`](docs/ACCESS_CONTROL.md) for identity, RBAC,
robot accounts, branch protection, break-glass access, and access reviews.
Use [`docs/CAPACITY_PLANNING.md`](docs/CAPACITY_PLANNING.md) to define
capacity domains, saturation signals, load tests, scaling decisions, and
private evidence.
Use [`docs/COMPLIANCE_AUDIT.md`](docs/COMPLIANCE_AUDIT.md) to map controls,
evidence records, audit logging, exceptions, and private review cadence.
Use [`docs/RELEASE_PROMOTION.md`](docs/RELEASE_PROMOTION.md) to define
environment promotion gates, rollback, hotfix, freeze, and release evidence.
Use [`docs/ALERTING.md`](docs/ALERTING.md) to define severity, receivers,
SLOs, routing tests, silences, and alert review evidence.
Use [`docs/THREAT_MODEL.md`](docs/THREAT_MODEL.md) to review assets, trust
boundaries, high-risk changes, and private-deployment security evidence.
Use [`docs/DATA_CLASSIFICATION.md`](docs/DATA_CLASSIFICATION.md) to classify
component data, retention decisions, disposal, and private evidence.

## Default architecture

```text
                     users / developers / operators
                                  |
                         <PLATFORM_VIP_DNS>
                                  |
                   VIP: kube-vip or HAProxy/Keepalived
                                  |
                         MetalLB service VIPs
                                  |
                         Traefik Ingress
                                  |
 ------------------------------------------------------------------
|                  3-node RKE2 Kubernetes cluster                  |
|                                                                  |
|  node-1                 node-2                 node-3             |
|  server + etcd          server + etcd          server + etcd      |
|  worker schedulable     worker schedulable     worker schedulable |
|                                                                  |
|  Forgejo/Gitea/GitLab   Woodpecker CI HA       Argo CD HA         |
|  Keycloak SSO           Harbor registry        CloudNativePG      |
|  Prometheus/Grafana     Loki                   Velero             |
 ------------------------------------------------------------------
                                  |
                    dev / stage / prod application namespaces
```

## Quick start

```bash
git clone <YOUR_REMOTE_URL> platform-gitops
cd platform-gitops

# Create local files. These files are ignored by git.
make init-local

# Edit placeholders only in local files.
${EDITOR:-vi} config/cluster.local.yaml
${EDITOR:-vi} inventory/hosts.local.ini

# Validate repository safety before first push.
make validate
make no-secrets
```

Then follow:

```text
docs/QUICK_START.md
docs/INSTALLATION.md
docs/ARCHITECTURE.md
```

## Supported systems

Use [`docs/PLATFORM_SUPPORT.md`](docs/PLATFORM_SUPPORT.md) for support tiers,
lifecycle rules, upgrade/deprecation policy, compatibility gates, and private
support evidence. Use [`docs/NODE_OS_SUPPORT.md`](docs/NODE_OS_SUPPORT.md) for
the cluster-node operating system matrix.

| Area | Supported target |
|---|---|
| Admin workstation | Windows, Windows Server, macOS, Linux, BSD-family systems, Solaris-family systems |
| Cluster nodes | Linux server distributions; see `docs/NODE_OS_SUPPORT.md` for tiers |
| Shell tooling | Bash-compatible shell plus PowerShell helper scripts |
| Git hosting compatibility | GitHub, GitLab, Gitea, Forgejo |
| CI config compatibility | GitHub Actions, GitLab CI, Gitea/Forgejo Actions, Woodpecker CI |

BSD and Solaris are supported as **operator/client workstations** for Git, SSH, editing, and documentation workflows. The Kubernetes server nodes should be Linux hosts. The default recommendation is Rocky Linux 10 on all three nodes. Other premium node targets are SLES/SLE Micro, RHEL, Oracle Linux, and Ubuntu Server LTS; Debian, AlmaLinux, CentOS Stream, Fedora, Arch, Gentoo, Linux Mint, and legacy CentOS are documented with compatible or lab/workstation guidance.

## Repository layout

```text
.
├── ansible/                    # Optional node preparation and RKE2 bootstrap playbooks
├── config/                     # Example global platform config, local files ignored
├── docs/                       # User guide, architecture, install, backups, i18n
├── examples/                   # App templates and 20 language scaffolds
├── gitops/                     # Argo CD app-of-apps and platform component manifests
├── inventory/                  # Example 3-node inventory, local files ignored
├── policies/                   # Optional guardrails for secret protection and safety
├── profiles/                   # Easy component-switching profiles
├── renovate.json               # Dependency dashboard, Helm grouping, image digest pinning
├── scripts/                    # Bootstrap, validation, and safety scripts
├── secrets/                    # README only; real secrets must never be committed
├── .github/                    # GitHub Actions validation
├── .gitea/                     # Gitea Actions validation
├── .forgejo/                   # Forgejo Actions validation
├── .woodpecker/                # Woodpecker CI validation
├── .gitlab-ci.yml              # GitLab CI validation
├── Makefile
├── LICENSE
└── README.md
```

## Component matrix

| Layer | Default | Alternatives included |
|---|---|---|
| Node OS | Rocky Linux 10 | SLES, SLE Micro, RHEL, Oracle Linux, Ubuntu Server LTS |
| Kubernetes | RKE2, 3 server nodes | Manual profile extension |
| CNI | Cilium | Canal, Calico, Flannel where supported by RKE2 |
| Git forge | Forgejo | Gitea, GitLab CE |
| CI | Woodpecker CI with HA server replicas and 3 Kubernetes agents | Gitea/Forgejo Actions, GitLab Runner profile |
| CD / GitOps | Argo CD HA | Kept as required deployment engine |
| Identity | Keycloak SSO | External OIDC/SAML identity providers |
| Registry | Harbor | Forgejo/Gitea packages or GitLab registry profile |
| Database | CloudNativePG PostgreSQL | External PostgreSQL profile |
| Storage | Longhorn | Rook/Ceph |
| Load balancer | MetalLB | External load balancer profile |
| Ingress | Traefik | ingress-nginx |
| TLS and trust | cert-manager + trust-manager | External certificate workflow, optional step-ca/internal CA |
| Monitoring | Prometheus + Grafana | Extendable |
| Logs | Loki | Extendable |
| Backups | Velero + DB backups + off-cluster target | External backup target profile |
| Object storage | Optional in-cluster MinIO for controlled internal S3 use | External S3 for production DR |
| API VIP | kube-vip | HAProxy + Keepalived |
| Policy | Kyverno audit baseline and examples | Other admission controllers |
| Runtime security | Tetragon eBPF observability | Falco or external runtime/SIEM integrations |
| Secrets | SOPS + age plus External Secrets Operator and OpenBao-ready backend | Sealed Secrets or external Vault-compatible/cloud secret manager |
| Supply chain | Cosign + Renovate helpers | Extendable |

## Recommended first repositories after deployment

```text
platform/platform-gitops     # this repository
platform/ci-templates        # shared CI logic
platform/cluster-bootstrap   # optional split-out bootstrap repo
gitops/apps-dev              # dev desired state
gitops/apps-stage            # staging desired state
gitops/apps-prod             # production desired state
apps/<service-name>          # each app source repository
```

The supply-chain helper surface includes `renovate.json` for dependency
dashboards, grouped Helm updates, and Docker digest pinning, plus
`policies/kyverno/verify-signed-images.example.yaml` for an opt-in Cosign image
signature verification policy after your CI signs and publishes images. Run
`make supply-chain-posture` when Syft is available to emit an SPDX SBOM under
`rendered/supply-chain`; it also records OpenSSF Scorecard output when the
`scorecard` binary is installed and verifies a signed image when
`COSIGN_IMAGE`/`COSIGN_PUBLIC_KEY` are set.

`make security-scan` runs Trivy, Gitleaks, and Semgrep. Semgrep defaults to the
checked-in `.semgrep.yml` baseline so private deployments can run reproducible
offline checks; set `SEMGREP_CONFIG=p/default` or another ruleset when you want
to opt into Semgrep registry rules on a connected runner.

For Git-stored private values, `config/sops.age.example.yaml` provides a
copyable SOPS + age starter policy. Replace the placeholder age recipient in a
private deployment repository and keep age private keys outside Git.

## International starter documentation

The `docs/i18n/` folder contains starter README files in 20 common global languages so teams can localize installation and governance documents faster.

## Multi-language app scaffolding

The `examples/languages/` folder contains starter service folders for 20 common programming languages. These are intentionally minimal so teams can adapt them to internal standards.

## License

0BSD. See [`LICENSE`](LICENSE).
