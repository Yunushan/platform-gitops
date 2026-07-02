# Platform Support

This document defines the public-safe support and lifecycle policy for this
platform template. Keep real vendor contracts, subscription IDs, internal
support contacts, asset inventories, ticket links, maintenance calendars, and
exception approvals in the private deployment repository or support system.

Use this document with [Node OS Support](NODE_OS_SUPPORT.md),
`docs/RELEASE_PROMOTION.md`, `docs/OPERATIONS.md`, and
`docs/PRODUCTION_READINESS.md`.

## Support Scope

The support model covers:

- Admin workstations used for Git, SSH, kubectl, Helm, documentation, and
  Argo CD CLI workflows.
- RKE2 server nodes and node preparation requirements.
- Git, CI, registry, storage, backup, observability, PKI, and ingress
  components deployed by the GitOps profiles.
- Repository validation, privacy validation, profile rendering, and live
  production gates.

It does not replace commercial vendor support for RKE2, Longhorn, operating
systems, Git hosting, registries, databases, storage, or backup targets.

## Support Tiers

| Tier | Meaning | Production use |
|---|---|---|
| Enterprise validated | Expected target for premium production profiles when versions, OS, kernel, and component prerequisites match the private support record. | Allowed after live gates and readiness evidence pass. |
| Compatible / best effort | Expected to work with documented preparation, but may not have the same upstream vendor validation or support contract coverage. | Allowed only with explicit private risk acceptance. |
| Lab or workstation only | Useful for administration, development, or lab clusters. | Not accepted for premium production server nodes. |
| Deprecated or unsupported | End-of-life, missing prerequisites, or known mismatch with RKE2, Longhorn, or platform requirements. | Block production launch until replaced or exception-approved. |

## Admin Workstations

Supported for repository management, documentation, kubectl, Helm, SSH, and
Argo CD CLI workflows:

- Windows
- Windows Server
- macOS
- Linux
- BSD-family systems
- Solaris-family systems

Admin workstations must keep Git credentials, kubeconfigs, SSH keys, age
private keys, CI tokens, and browser sessions under the private deployment
owner's security policy.

## Cluster Nodes

RKE2 Kubernetes server nodes should be Linux hosts. See
[Node OS Support](NODE_OS_SUPPORT.md) for the full node operating system
matrix, package prerequisites, premium recommendations, and lifecycle review
requirements.

Recommended premium node operating systems:

- Rocky Linux 10 or the currently validated Rocky/RHEL-compatible release
- SUSE Linux Enterprise Server
- SLE Micro
- Red Hat Enterprise Linux
- Oracle Linux
- Ubuntu Server LTS

Compatible or lab-profile node operating systems:

- AlmaLinux
- Debian Stable
- CentOS Stream
- Fedora Server
- Arch Linux
- Gentoo Linux
- Linux Mint
- Legacy CentOS Linux

Linux Mint, Arch, Gentoo, Fedora, CentOS Stream, and legacy CentOS are better
supported as operator workstations or lab nodes than premium production RKE2
server nodes.

## Component Support Matrix

| Area | Supported model |
|---|---|
| Kubernetes | RKE2 with etcd quorum and one-node maintenance tolerance for the premium 3-node profile |
| Networking | Cilium, CoreDNS, kube-proxy service path, kube-vip API VIP, and MetalLB app VIP |
| Ingress | Traefik by default; ingress-nginx can be selected through supported profiles |
| GitOps | Argo CD with private repository source and validated Application manifests |
| Source control | Forgejo by default, with Gitea or GitLab CE as supported alternatives |
| CI | Woodpecker by default, GitLab Runner as an alternative when GitLab CE is selected |
| Registry | Harbor with externalized storage, database, Redis, TLS, retention, and scanning reviewed privately |
| Databases | CloudNativePG for in-cluster PostgreSQL patterns when selected by the profile |
| Storage | Longhorn by default for premium profile, Rook Ceph as an alternative profile |
| Backup | Velero with off-cluster object storage and restore drill evidence |
| Observability | Prometheus, Grafana, and Loki with retention, alerting, and SLO review |
| PKI and trust | cert-manager, trust-manager, and optional step-ca when enabled |
| Policy | Public Kyverno examples for baseline, no-plaintext-secret, and signed-image policy adaptation |

## Git and CI Compatibility

Supported Git hosting workflows:

- GitHub
- GitLab
- Gitea
- Forgejo

Included validation configs:

```text
.github/workflows/validate.yml
.gitlab-ci.yml
.gitea/workflows/validate.yml
.forgejo/workflows/validate.yml
.woodpecker/validate.yml
```

## Version and Lifecycle Policy

- Chart versions, image tags, and selected CI actions should remain pinned and
  changed intentionally through a pull request.
- Production deployments should use upstream-supported RKE2, operating system,
  Longhorn, Argo CD, Harbor, Forgejo, Woodpecker, Velero, cert-manager, and
  observability versions.
- Private deployments should review component lifecycle at least quarterly and
  before every production release.
- End-of-life operating systems, kernels, Kubernetes versions, storage engines,
  or application versions must be upgraded, isolated to a lab profile, or
  carried as an explicit exception with owner, expiration, and compensating control.
- Vulnerability advisories and breaking changes should feed
  `docs/RELEASE_PROMOTION.md`, `docs/THREAT_MODEL.md`, and `SECURITY.md`.

## Compatibility Gates

Before accepting a supported combination for production, capture evidence for:

```bash
python scripts/run_validation.py
make no-secrets
PLATFORM_PROFILE=<PROFILE> make platform-profile-check
make rke2-verify
make platform-status
make platform-app-health
PLATFORM_PROFILE=<PROFILE> make platform-production-check
```

Also complete the restore drill in `docs/BACKUP_RESTORE.md`, capacity review in
`docs/CAPACITY_PLANNING.md`, alert/SLO review in `docs/ALERTING.md`, and final
go/no-go checklist in `docs/PRODUCTION_READINESS.md`.

## Upgrade and Deprecation Policy

Use `docs/RELEASE_PROMOTION.md` for version upgrades and component replacement.
Every upgrade or deprecation should record:

- Component and current version.
- Target version or replacement.
- Support tier before and after the change.
- Owner and approval authority.
- Breaking changes and migration steps.
- Backup and restore evidence.
- rollback or roll-forward plan.
- Health gates and post-change monitoring window.
- Deprecation deadline for old components or profiles.

Do not remove a supported profile, component, or operating system tier until the
replacement path, migration notes, and private user impact are reviewed.

## Support Evidence

Private deployments should keep a current support record with:

- Node operating system, kernel, package baseline, and node preparation date.
- RKE2, Cilium, Longhorn or storage, ingress, Argo CD, Git, CI, registry,
  database, backup, observability, and PKI versions.
- Profile name and rendered private values source.
- Latest validation and live health gate output.
- Open exceptions, expiration dates, compensating controls, and owners.
- Vendor support status or internal acceptance for every enterprise-critical
  component.

Do not commit private support inventories, vendor ticket links, subscription
IDs, internal hostnames, IP addresses, usernames, or approval records to this
public template.

## Out of Scope

The public template does not provide:

- A commercial support entitlement.
- Production warranty for unsupported operating systems or end-of-life
  components.
- A replacement for private change management, incident response, legal review,
  compliance audit, or vendor support processes.
- Proof that a private environment is production-ready without live validation
  and private evidence.
