# Architecture

## Goal

Provide a zero-subscription, private-first CI/CD and GitOps platform for individuals and companies using a compact 3-node RKE2 Kubernetes cluster.

## Default components

| Layer | Component |
|---|---|
| Node OS | Rocky Linux 10 |
| Kubernetes | RKE2, 3 server nodes |
| CNI | Cilium |
| API VIP | kube-vip default, HAProxy/Keepalived alternative |
| Git forge | Forgejo |
| CI | Woodpecker CI with HA server replicas and distributed Kubernetes agents |
| CD | Argo CD HA |
| Identity | Keycloak SSO for central identity and RBAC integration |
| Registry | Harbor |
| Database | CloudNativePG PostgreSQL |
| Storage | Longhorn default, Rook/Ceph alternative |
| LoadBalancer services | MetalLB |
| Ingress | Traefik default, ingress-nginx alternative |
| TLS and trust | cert-manager, trust-manager, optional step-ca/internal CA |
| Monitoring | Prometheus + Grafana |
| Logs | Loki |
| Backups | Velero plus database and off-cluster backups |
| Secrets | SOPS + age, External Secrets Operator, and internal OpenBao-ready backend |
| Policy | Kyverno audit baseline and examples |
| Runtime security | Tetragon eBPF observability |
| Supply chain | Trivy, Gitleaks, Semgrep, Cosign, and Renovate helpers |

## Network model

```text
<PLATFORM_VIP_DNS>
  -> API VIP provider for Kubernetes API
  -> MetalLB service VIPs for ingress services
  -> Traefik
  -> platform services and application namespaces
```

## Premium 3-node profile

The premium profile is available at `profiles/premium-3node.yaml` and deploys from `gitops/clusters/rke2-main/premium-3node`.

It keeps the same recommended stack and adds hardened values for storage, backups, observability, and HA dependencies.

## PKI Model

cert-manager handles certificate lifecycle for Kubernetes workloads and
ingress certificates. trust-manager distributes trust bundles, including public
roots by default and optional organization roots from private overlays.

step-ca is optional. It is useful when the organization needs an internal CA
for private service certificates, mTLS, or offline/private environments. The
upstream `step-certificates` chart supports one CA replica, so production
resilience depends on durable storage, backup/restore, and root/intermediate
key handling rather than multiple CA pods.

## CI/CD HA model

The recommended platform keeps CI and CD separate:

- Forgejo stores Git repositories and pull requests.
- Woodpecker CI runs build, test, scan, sign, and image-publish workflows.
- Argo CD deploys the desired Kubernetes state from GitOps repositories.

In the 3-node profiles, Woodpecker is not intended to run as a single-node-only CI service. The chart values run multiple Woodpecker server replicas behind Kubernetes Service/Ingress and three Kubernetes agents so jobs can continue when one worker node is unavailable. Woodpecker HA requires shared PostgreSQL state and identical server secrets across replicas.

Argo CD uses the HA profile. The premium profile runs multiple `argocd-server`, `argocd-repo-server`, and `argocd-applicationset-controller` replicas, with Redis HA enabled. Argo CD stores desired state in Kubernetes objects backed by RKE2 etcd, so the cluster's 3 server nodes provide the control-plane quorum.

## Security model

- No production secret is committed to git.
- Real environment data exists only in ignored local files or external secret systems.
- The premium profile includes Keycloak as the central FOSS SSO plane so
  Forgejo, Woodpecker, Argo CD, Harbor, Grafana, and future apps can move away
  from local-only credentials when OAuth/OIDC integration is enabled.
- The premium profile installs External Secrets Operator and an internal
  OpenBao HA Raft backend so private deployments can move generated bootstrap
  Kubernetes Secrets toward a managed secret-store flow without leaving the
  FOSS stack.
- Production deployment is driven by Argo CD from GitOps repositories.
- CI builds artifacts and updates desired state; it does not directly deploy production.
- Renovate tracks dependency update drift through `renovate.json`, with Docker
  digest pinning and dashboard approval for major changes.
- Trivy, Gitleaks, and Semgrep are exposed through `make security-scan` for
  deeper repository and supply-chain checks outside the fast validation suite.
  Semgrep defaults to the checked-in `.semgrep.yml` baseline for reproducible
  offline/private scans; connected runners can override `SEMGREP_CONFIG` to use
  Semgrep registry packs.
- Tetragon is included in the premium profile for eBPF process, file, network,
  credential, namespace, and policy-filter observability on the RKE2 nodes.
- Syft SBOM generation, optional OpenSSF Scorecard output, and optional Cosign
  verification are exposed through `make supply-chain-posture`.
- Cosign image signature verification is provided as an opt-in Kyverno example;
  enable it only after CI signs images and registry credentials/key material are
  available in the target namespaces.
- Production threat modeling is documented in `docs/THREAT_MODEL.md`, including
  assets, trust boundaries, high-risk changes, and private evidence.

## Failure model

A 3-node cluster is designed for one-node failure. It is not a replacement for
multi-site disaster recovery. Off-cluster backups, restore drill evidence, and
business continuity planning are mandatory; use `docs/BACKUP_RESTORE.md` and
`docs/BUSINESS_CONTINUITY.md` as production acceptance runbooks.

## Operations Model

Day-2 operations are governed by `docs/OPERATIONS.md`: ownership, change
management, maintenance windows, break-glass access, incident response, drift
management, credential rotation, capacity tracking, and production evidence.
Service ownership, criticality, dependencies, SLO/SLA expectations, data
classification, and recovery metadata are governed by
`docs/SERVICE_CATALOG.md`.
Significant platform architecture choices and their consequences should be
recorded with `docs/ARCHITECTURE_DECISIONS.md`.
Final launch acceptance, go/no-go evidence, exceptions, and post-launch
validation are governed by `docs/PRODUCTION_READINESS.md`.
Detailed incident declaration, roles, communications, recovery validation, and
post-incident review are governed by `docs/INCIDENT_RESPONSE.md`.
Identity, RBAC, admin roles, robot accounts, branch protection, break-glass
access, and access-review evidence are governed by `docs/ACCESS_CONTROL.md`.
Capacity domains, saturation signals, load tests, scaling decisions, and
private capacity evidence are governed by `docs/CAPACITY_PLANNING.md`.
Control domains, audit evidence, exception handling, and compliance review
cadence are governed by `docs/COMPLIANCE_AUDIT.md`.
Release and environment promotion gates, rollback, hotfixes, freezes, and
release evidence are governed by `docs/RELEASE_PROMOTION.md`.
Alert severity, routing, SLO/error budget, silence, and receiver-test
expectations are governed by `docs/ALERTING.md`.
