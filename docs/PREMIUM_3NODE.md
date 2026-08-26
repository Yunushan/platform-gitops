# Premium 3-Node Profile

The premium profile keeps the compact 3-node RKE2 footprint, but uses stricter defaults for production-like private deployments.

## What changes

| Area | Default profile | Premium profile |
|---|---|---|
| Bootstrap path | `gitops/clusters/rke2-main` | `gitops/clusters/rke2-main/premium-3node` |
| Node OS | Rocky Linux 10 | Rocky Linux 10 |
| CNI | Cilium | Cilium |
| Ingress | Traefik | Traefik with HA/resource values |
| Storage | Minimal Longhorn values | Longhorn storage classes and backup target placeholders |
| Registry | Basic Harbor values | Harbor HA values with external PostgreSQL, Redis, and object storage placeholders |
| Database | CloudNativePG operator only | CloudNativePG operator plus a 3-instance cluster example with WAL archive |
| Shared cache | App-local cache defaults | Shared Valkey HA for platform apps, separate from Argo CD Redis |
| CI | Woodpecker CI | Woodpecker server replicas plus 3 Kubernetes agents |
| CD / GitOps | Argo CD HA | Argo CD HA with multi-replica server, repo server, ApplicationSet, and Redis HA |
| Identity | Local app credentials | Keycloak SSO on `sso.<PLATFORM_DOMAIN>` backed by CloudNativePG |
| Backups | Velero placeholders | Velero schedule, CSI snapshot, and off-cluster object storage placeholders |
| Observability | Minimal values | HA Prometheus, routed Alertmanager, Grafana SSO, authenticated retained Loki, clustered Alloy collection, and delivery proof |
| Object storage | External S3 placeholders | Maintained external/off-cluster S3 required; archived MinIO is excluded |
| Secrets | SOPS + age examples | External Secrets Operator plus OpenBao HA Raft backend for private secret workflows |
| Security | Optional policy examples | Kyverno stable CEL baseline, staged signed-image admission, Tetragon runtime observability, and managed NetworkPolicy |

## Activation

Use the premium profile when registering platform applications in Argo CD:

```bash
PLATFORM_PROFILE=premium-3node PLATFORM_APPLY_GITOPS=true PLATFORM_REPO_URL=<PRIVATE_REPO_URL> make platform-argocd
```

That registers applications from:

```text
gitops/clusters/rke2-main/premium-3node
```

The profile metadata is stored in:

```text
profiles/premium-3node.yaml
config/cluster.premium.example.yaml
```

## Required private values

Before applying the profile, replace placeholders through private overlays, encrypted secrets, or Argo CD parameters. Do not commit the real values.

Required private inputs include:

- Platform domain and TLS issuers.
- MetalLB address pool.
- Longhorn backup target and credentials.
- Velero object storage bucket and credentials.
- CloudNativePG object storage bucket and credentials.
- Harbor external PostgreSQL, Redis, and object storage settings.
- Forgejo database, Redis, and repository backup settings.
- Keycloak SSO host, admin secret, and CloudNativePG database secret settings.
- Woodpecker OAuth and agent settings.
- Grafana, Prometheus, and Loki storage sizes, Loki retention and gateway credentials, an Alertmanager receiver, plus Grafana external PostgreSQL settings for production monitoring. See `docs/OBSERVABILITY.md`.

`make platform-render-private-values` can render first-deploy private values
for Forgejo, Argo CD, Woodpecker, Keycloak, Harbor, Grafana, Prometheus, Loki, Velero,
CloudNativePG PostgreSQL cluster examples, Longhorn, and optional step-ca from `inventory/hosts.local.ini` plus
environment variables. Runtime credentials still stay out of Git:
`make platform-app-secrets` can create shared platform Valkey auth, Grafana
admin credentials, Grafana's external PostgreSQL password, Keycloak admin and
database credentials, the Woodpecker
PostgreSQL datasource, Harbor external PostgreSQL/Redis/S3 credentials, Loki,
Velero, and CloudNativePG Kubernetes secrets from
`FORGEJO_DATABASE_PASSWORD`, `FORGEJO_REDIS_URL`,
`FORGEJO_S3_ACCESS_KEY_ID` / `FORGEJO_S3_SECRET_ACCESS_KEY`,
`WOODPECKER_DATABASE_DATASOURCE`,
`WOODPECKER_DATABASE_HOST` / `WOODPECKER_DATABASE_PASSWORD`,
`HARBOR_DATABASE_PASSWORD`, `HARBOR_REDIS_PASSWORD`,
`HARBOR_S3_ACCESS_KEY_ID` / `HARBOR_S3_SECRET_ACCESS_KEY`,
`KEYCLOAK_ADMIN_PASSWORD`, `KEYCLOAK_DATABASE_PASSWORD`,
`GRAFANA_DATABASE_PASSWORD`, `LOKI_S3_ACCESS_KEY_ID` / `LOKI_S3_SECRET_ACCESS_KEY`,
`VELERO_CLOUD_CREDENTIALS`, `CNPG_S3_ACCESS_KEY_ID` /
`CNPG_S3_SECRET_ACCESS_KEY`, or `AWS_ACCESS_KEY_ID` /
`AWS_SECRET_ACCESS_KEY`. It also creates `object-storage/minio-root` for the
optional MinIO app; override `MINIO_ROOT_USER` / `MINIO_ROOT_PASSWORD` or keep
`MINIO_ROOT_AUTO_GENERATE=true` for a generated bootstrap password. For production, set
`PLATFORM_APP_SECRET_REQUIRE_OBJECT_STORAGE=true` so missing Loki, Velero, or CloudNativePG
object-storage credential secrets fail during secret automation instead of
surfacing later as unhealthy pods.
Set `PLATFORM_APP_SECRET_REQUIRE_CNPG_OBJECT_STORAGE=true` when you only want to
enforce the CloudNativePG backup credential secret without enforcing the rest of
the object-storage stack.
`CNPG_RENDER_POSTGRES_CLUSTER=true` is the default so
`make platform-render-private-values` materializes the premium CloudNativePG
PostgreSQL cluster used by Forgejo. Set `CNPG_RENDER_POSTGRES_CLUSTER=false`
only when you provide PostgreSQL outside this profile. Set
`CNPG_BACKUP_ENABLED=true` with CloudNativePG object-storage credentials when
you want the rendered cluster to include WAL/archive backups.
Set `PLATFORM_APP_SECRET_REQUIRE_HARBOR_DATABASE=true`,
`PLATFORM_APP_SECRET_REQUIRE_HARBOR_REDIS=true`, and
`PLATFORM_APP_SECRET_REQUIRE_HARBOR_REGISTRY_STORAGE=true` before enabling
Harbor external PostgreSQL, Redis, and S3 registry storage.
Set `PLATFORM_APP_SECRET_REQUIRE_FORGEJO_DATABASE=true` before enabling an
external Forgejo SQL backend. Set `PLATFORM_APP_SECRET_REQUIRE_FORGEJO_REDIS=true`
only when `FORGEJO_REDIS_MODE=redis`.
Set `PLATFORM_APP_SECRET_REQUIRE_FORGEJO_OBJECT_STORAGE=true` for production
Forgejo. Strict rendering requires HTTPS S3-compatible object storage for
attachments, LFS, avatars, and packages; credentials are kept in the managed
`forgejo-object-storage` Secret and never rendered into Git.
Set `PLATFORM_APP_SECRET_REQUIRE_GRAFANA_DATABASE=true` before enabling
`GRAFANA_DATABASE_MODE=postgres`.
Set `PLATFORM_APP_SECRET_REQUIRE_KEYCLOAK_DATABASE=true` only when you require
a specific Keycloak database password instead of the generated first-deploy
secret.

Forgejo defaults to PostgreSQL through the CloudNativePG service. Use
`FORGEJO_DATABASE_MODE=sqlite` only for lab bootstrap, or
`FORGEJO_DATABASE_MODE=mysql` / `FORGEJO_DATABASE_MODE=mariadb` with
`FORGEJO_DATABASE_HOST=<HOST>:3306` when that database family is the company
standard. Keep the rendered values free of credentials by creating dependency
secrets first, then render only endpoints and Secret names:

```bash
FORGEJO_DATABASE_PASSWORD='<PASSWORD>' \
PLATFORM_APP_SECRET_REQUIRE_FORGEJO_DATABASE=true \
make platform-app-secrets

FORGEJO_DATABASE_MODE=postgres \
FORGEJO_DATABASE_HOST=<POSTGRES_HOST>:5432 \
FORGEJO_DATABASE_NAME=forgejo \
FORGEJO_DATABASE_USER=forgejo \
FORGEJO_DATABASE_SECRET_NAME=forgejo-database \
FORGEJO_DATABASE_SSL_MODE=verify-full \
make platform-render-private-values
```

For Redis-backed cache and queue, the premium default is
`FORGEJO_REDIS_MODE=redis` using the shared `platform-valkey` HA service.
`platform-app-secrets` creates `platform-cache/platform-valkey-auth`, then
derives the `forgejo/forgejo-redis` URI secret. To use another cache, provide
`FORGEJO_REDIS_URL`, or provide `FORGEJO_REDIS_HOST` plus
`FORGEJO_REDIS_PASSWORD` and optional `FORGEJO_REDIS_PORT`, `FORGEJO_REDIS_DB`,
and `FORGEJO_REDIS_TLS`. Managed Valkey transport is TLS-only; production-strict
rendering rejects `FORGEJO_REDIS_TLS=false`.

Forgejo's production asset storage is configured separately from its SQL and
cache dependencies:

```bash
FORGEJO_S3_ACCESS_KEY_ID='<ACCESS_KEY>' \
FORGEJO_S3_SECRET_ACCESS_KEY='<SECRET_KEY>' \
PLATFORM_APP_SECRET_REQUIRE_FORGEJO_OBJECT_STORAGE=true \
make platform-app-secrets

FORGEJO_OBJECT_STORAGE_MODE=s3 \
FORGEJO_S3_ENDPOINT=https://<S3_ENDPOINT> \
FORGEJO_S3_REGION=<S3_REGION> \
FORGEJO_S3_BUCKET=platform-forgejo \
FORGEJO_S3_SECRET_NAME=forgejo-object-storage \
FORGEJO_S3_SECURE=true \
make platform-render-private-values
```

The renderer maps Forgejo attachments, LFS, avatars, and packages to S3 and
rejects a cluster-local endpoint or plaintext HTTP in production-strict mode.
The filesystem mode remains available only with
`PLATFORM_PRODUCTION_STRICT=false` for lab bootstrap. Forgejo remains a
single-replica workload until its repository storage and restore process are
proven independently; object storage removes the shared-asset bottleneck but
does not make the Forgejo SQL/SSH process itself active-active.

## CI/CD high availability

The premium profile keeps Forgejo as the Git forge and uses Woodpecker plus Argo CD for CI/CD. Forgejo remains single-replica until repository storage, SSH access, and restore procedures are proven. It uses a `Recreate` update strategy to prevent concurrent access to its RWO volume and a `minAvailable: 1` PodDisruptionBudget to block unapproved voluntary eviction, but node loss or an intentional upgrade still causes a service interruption. This does not mean CI/CD is single-node.

Woodpecker is configured for the 3-node cluster with:

- `server.statefulSet.replicaCount: 3` for the Woodpecker web/API service.
- `agent.replicas: 3` for Kubernetes-backed build agents.
- Explicit server and agent image repositories plus `WOODPECKER_IMAGE_TAG`, defaulting to `v3.16.0`.
- PostgreSQL-backed state through `WOODPECKER_DATABASE_DRIVER=postgres`.
- A 60-minute default and 120-minute maximum pipeline timeout, enforced by the Woodpecker server.
- Traefik ingress at the effective CI hostname, defaulting to `woodpecker.<PLATFORM_DOMAIN>` unless `platform_ci_host` or `platform_woodpecker_host` is set.

The first-deploy renderer defaults Woodpecker to PostgreSQL HA against the
shared `platform-postgres` CloudNativePG cluster. `platform-app-secrets`
generates `woodpecker/woodpecker-database` and the matching
`platform-databases/woodpecker-database` role password secret unless you provide
`WOODPECKER_DATABASE_PASSWORD` or a full `WOODPECKER_DATABASE_DATASOURCE`. It
also generates and preserves `woodpecker/woodpecker-agent-secret`; both server
and agents consume that one token, while chart-side random generation is
disabled for deterministic Argo CD rendering.
Render with:

```bash
WOODPECKER_DATABASE_MODE=postgres \
WOODPECKER_DATABASE_SECRET_NAME=woodpecker-database \
WOODPECKER_AGENT_SECRET_NAME=woodpecker-agent-secret \
WOODPECKER_DATABASE_HOST=platform-postgres-rw.platform-databases.svc.cluster.local:5432 \
WOODPECKER_DATABASE_NAME=woodpecker \
WOODPECKER_DATABASE_USER=woodpecker \
WOODPECKER_DATABASE_SSLMODE=verify-full \
WOODPECKER_DATABASE_SSLROOTCERT=/etc/ssl/platform-postgres/ca-certificates.crt \
PLATFORM_APP_SECRET_REQUIRE_WOODPECKER_DATABASE=true \
make platform-app-secrets

WOODPECKER_DATABASE_MODE=postgres \
WOODPECKER_DATABASE_SECRET_NAME=woodpecker-database \
WOODPECKER_AGENT_SECRET_NAME=woodpecker-agent-secret \
WOODPECKER_IMAGE_TAG=v3.16.0 \
WOODPECKER_SERVER_REPLICAS=3 \
WOODPECKER_AGENT_REPLICAS=3 \
make platform-render-private-values
```

For a small non-HA bootstrap, set `WOODPECKER_DATABASE_MODE=sqlite` and keep
`WOODPECKER_SERVER_REPLICAS=1`.

Harbor also has a bootstrap-first default: internal PostgreSQL, shared
external `platform-valkey`, and filesystem registry storage. For the premium
production shape, create the secret-backed external dependencies first, then
render values that reference only Secret names and non-secret endpoints. If
you keep the shared Valkey default, `platform-app-secrets` derives
`harbor/harbor-redis` from `platform-cache/platform-valkey-auth`; override
`HARBOR_REDIS_ADDR` and `HARBOR_REDIS_PASSWORD` only for a separate Redis or
Valkey endpoint. `HARBOR_REDIS_TLS` defaults to `true` and cannot be disabled in
production-strict rendering:

```bash
HARBOR_DATABASE_PASSWORD='<PASSWORD>' \
HARBOR_REDIS_PASSWORD='<PASSWORD>' \
HARBOR_S3_ACCESS_KEY_ID='<ACCESS_KEY>' \
HARBOR_S3_SECRET_ACCESS_KEY='<SECRET_KEY>' \
PLATFORM_APP_SECRET_REQUIRE_HARBOR_DATABASE=true \
PLATFORM_APP_SECRET_REQUIRE_HARBOR_REDIS=true \
PLATFORM_APP_SECRET_REQUIRE_HARBOR_REGISTRY_STORAGE=true \
make platform-app-secrets

HARBOR_DATABASE_MODE=external \
HARBOR_DATABASE_HOST=<POSTGRES_HOST> \
HARBOR_DATABASE_SECRET_NAME=harbor-database \
HARBOR_DATABASE_SSLMODE=verify-full \
HARBOR_REDIS_MODE=external \
HARBOR_REDIS_ADDR=platform-valkey-primary.platform-cache.svc.cluster.local:6379 \
HARBOR_REDIS_SECRET_NAME=harbor-redis \
HARBOR_STORAGE_MODE=s3 \
HARBOR_S3_BUCKET=<HARBOR_REGISTRY_BUCKET> \
HARBOR_S3_SECRET_NAME=harbor-registry-s3 \
OBJECT_STORAGE_ENDPOINT=<S3_ENDPOINT> \
OBJECT_STORAGE_REGION=<S3_REGION> \
make platform-render-private-values
```

Grafana defaults to persistent SQLite for first bootstrap. For production
monitoring, create the external database password secret and render Grafana with
PostgreSQL-backed state:

```bash
GRAFANA_DATABASE_PASSWORD='<PASSWORD>' \
PLATFORM_APP_SECRET_REQUIRE_GRAFANA_DATABASE=true \
make platform-app-secrets

GRAFANA_DATABASE_MODE=postgres \
GRAFANA_DATABASE_HOST=<POSTGRES_HOST> \
GRAFANA_DATABASE_NAME=grafana \
GRAFANA_DATABASE_USER=grafana \
GRAFANA_DATABASE_SECRET_NAME=grafana-database \
GRAFANA_DATABASE_SSL_MODE=verify-full \
make platform-render-private-values
```

Argo CD HA is configured with:

- `server.replicas: 3` in the premium values.
- `repoServer.replicas: 3` in the premium values.
- `applicationSet.replicas: 2`.
- `redis-ha.enabled: true`.

That Argo CD Redis HA deployment stays dedicated to Argo CD. Platform
applications use the separate `platform-valkey` HA service in `platform-cache`.

With the 3 RKE2 server nodes healthy, these services are scheduled across the cluster and continue operating through a single-node failure, assuming storage, database, DNS, ingress VIP, and off-cluster backups are correctly configured.

## Policy, runtime security, and object storage

The premium profile installs Kyverno with audit-first defaults, then registers
`platform-policies` for baseline workload and private Secret workflow checks.
The three managed policies use Kyverno 1.18's stable
`policies.kyverno.io/v1` CEL API and intentionally start with
`validationActions: [Audit]`. Set `PLATFORM_POLICY_ENFORCEMENT=Enforce` only
after reports are clean; private rendering maps that value to the stable `Deny`
action.

An upgrade from the former `kyverno.io/v1` ClusterPolicies is deliberately a
reviewed migration. Confirm all three replacement ValidatingPolicies are Ready,
then approve the guarded Argo CD prune for the two superseded ClusterPolicies.
`make platform-policy-readiness` rejects a partially migrated state.

The separate `platform-image-integrity` Application uses Kyverno 1.18's stable
`ImageValidatingPolicy` API. It remains absent while
`PLATFORM_IMAGE_INTEGRITY_MODE=disabled`, starts in `Audit` after a private
registry and approved Cosign PEM public key are rendered, and moves to `Deny`
only for production. The production readiness gate requires
`PLATFORM_IMAGE_INTEGRITY_CANARY_IMAGE` as a genuinely signed digest, admits it
with server-side dry-run, derives an invalid digest, and proves that Kyverno
rejects the invalid form. Configuration and key-rotation steps are in
`docs/SUPPLY_CHAIN.md`.

Managed application namespaces declare Pod Security Admission intent instead
of inheriting an implicit cluster default. External Secrets and OpenBao enforce
`restricted`; ordinary controllers and stateful platform namespaces enforce
`baseline` while auditing and warning on `restricted`.

Explicit privileged namespaces are limited to `longhorn-system`,
`metallb-system`, `monitoring`, `tetragon`, and `velero`. These stacks need host
storage, host networking, node metrics, eBPF, or filesystem-backup access that
the baseline profile intentionally rejects. `kube-system` is the corresponding
RKE2-managed exception. The Pod-security ValidatingPolicy excludes exactly those
namespaces while the workload resource-request policy still applies to their
controllers. Every other AppProject destination must have a namespace manifest
with a baseline or restricted enforcement level, and validation fails when a
new destination is left unclassified. Semgrep's privileged-container exceptions
are limited to the three exact Longhorn 1.12.0 host-storage templates and the
exact Tetragon 1.6.0 values file that enables required eBPF host access; changing
either vendored chart version therefore requires an explicit exception review.

The profile also installs Tetragon in a dedicated privileged namespace for
Cilium/eBPF runtime observability. Tetragon exports process and policy events,
enables Prometheus ServiceMonitor resources, redacts common password/token
arguments from exported events, and keeps runtime hooks disabled by default.
Promote runtime enforcement deliberately after observing normal workload
behavior and documenting allowed processes in the private deployment repo.

The production `premium-3node` application set does not register MinIO because
the upstream MinIO server project is archived and no longer receives normal
maintenance. Use maintained external/off-cluster S3-compatible storage for
Velero, CloudNativePG WAL archives, Harbor registry objects, and Loki chunks.

`PLATFORM_PROFILE=premium-3node-lab` adds the retained in-cluster MinIO chart
for isolated labs and migration testing only. It is not a production or
disaster-recovery profile. When moving an existing installation to the
production profile, migrate all objects first, verify restores from the new
target, and only then approve Argo CD's protected `Prune=confirm` removal.

## Secret management

The premium profile keeps SOPS + age as the Git-friendly encrypted secret
starter and adds External Secrets Operator plus OpenBao for a FOSS
secret-management path. External Secrets installs CRDs and reconciles
`SecretStore`, `ClusterSecretStore`, and `ExternalSecret` resources into
Kubernetes Secrets. OpenBao is deployed internally in HA Raft mode with
retained Longhorn data and audit PVCs, but it is not exposed through public
ingress by default.

The Raft configuration declares TLS-verified `retry_join` targets for all three
StatefulSet ordinals. After the first server is initialized and unsealed, later
ordinals join the same Raft cluster automatically; with the default Shamir seal,
each joined server must still be unsealed by the approved key custodians.

Use `make platform-openbao-status` for a sanitized diagnostic that does not
require a token and does not expose the cluster identifier. Production
acceptance uses `make platform-openbao-verify` and fails unless at least three
current and Ready replicas are initialized, unsealed, HA-enabled, and report one
shared cluster identity. The verifier hashes that identity before printing it.

OpenBao agent injection is fail-closed and namespace opt-in. Enable it only
after the injector has ready endpoints by labeling an application namespace:

```bash
kubectl label namespace APPLICATION_NAMESPACE \
  platform.gitops/openbao-injection=enabled
```

The `kube-system`, `kube-public`, `kube-node-lease`, and `openbao` namespaces
are always excluded so an unavailable optional injector cannot block cluster
DNS, admission recovery, or its own pods. Pods in an opted-in namespace remain
blocked when the injector is unavailable, which prevents workloads from
starting without their requested secrets.

Before production use, complete the private OpenBao ceremony outside this
public template: initialize and unseal, store recovery material in an approved
offline/HSM/KMS process, enable Kubernetes auth, create least-privilege
policies, and migrate generated bootstrap secrets into ExternalSecret
definitions. Keep OpenBao policies, tokens, unseal material, and backend
credentials in the private deployment repository or a controlled secret system.
Follow `docs/OPENBAO_CEREMONY.md`; production evidence rejects missing, stale,
self-approved, plaintext, weak-quorum, configuration-mismatched, or
cluster-mismatched ceremony records.

Run deeper repository security checks with:

```bash
make security-scan
```

That wrapper expects Trivy, Gitleaks, and Semgrep on the runner and keeps normal
`make validate` fast. Semgrep uses the checked-in `.semgrep.yml` baseline by
default so private runners can scan without fetching remote rules; set
`SEMGREP_CONFIG=p/default` or another ruleset when you intentionally want remote
registry rules.

Generate supply-chain posture artifacts with:

```bash
make supply-chain-posture
```

That target writes an SPDX SBOM with Syft under `rendered/supply-chain`, captures
OpenSSF Scorecard output when `scorecard` is installed, and can verify a signed
image with Cosign when `COSIGN_IMAGE` and `COSIGN_PUBLIC_KEY` are set.

Production promotion uses the strict form:

```bash
COSIGN_IMAGES_FILE=private/supply-chain/cosign-images.txt \
make supply-chain-verify
```

It blocks on scanner findings, malformed or empty SPDX evidence, a Scorecard
below the configured threshold, tag-only image references, and failed Cosign
verification. See `docs/SUPPLY_CHAIN.md` for the inventory format and limits.

## Storage stance

The premium profile keeps Longhorn instead of switching to Rook/Ceph. For only
three nodes, Longhorn is the more practical default. The profile creates:

- `longhorn-standard-encrypted` with 2 replicas for ordinary stateful workloads.
- `longhorn-critical-encrypted` with 3 replicas for critical platform state.
- `longhorn-cache-encrypted` with 1 replica for rebuildable cache data.
- Legacy unencrypted class names remain non-default only so existing PVCs keep
  their immutable identity during a controlled migration.

`make platform-app-secrets` creates and preserves the
`longhorn-system/longhorn-crypto` LUKS key before PVC provisioning. Keep an
encrypted recovery copy outside the cluster. The general app-secret rotation
switch intentionally never replaces this key. Every RKE2 node must have
`cryptsetup` and `dm_crypt`; node preparation installs and verifies both.

Kubernetes cannot encrypt an existing PVC in place. Upgrade by restoring each
workload into a new PVC that selects an encrypted class, validate application
data, then retire the old PVC only after rollback and retention windows expire.
The production capacity gate fails while any bound Longhorn PVC still uses a
legacy class or lacks encrypted CSI attributes and Secret references.

Use dedicated disks for Longhorn and configure node or disk labels before production.

## Production gate

Do not call the platform production-ready until these checks pass:

1. One-node failure test.
2. Node reboot and upgrade test.
3. Longhorn volume restore test.
4. CloudNativePG PITR or snapshot restore test.
5. Velero namespace restore test.
6. Harbor image push, pull, scan, retention, and restore test.
7. Forgejo repository backup and restore test.
8. Alert delivery test.
9. Platform app production gate: required Argo CD Applications Synced/Healthy
   with no active or failed operations, platform pods Ready, required Longhorn
   StorageClasses present, critical HA workloads meeting minimum desired and
   ready replica coverage, platform PVCs Bound, CloudNativePG PostgreSQL
   clusters Ready when present or explicitly required, Argo CD Applications
   sourced from the intended production Git repository instead of temporary seed
   Git or insecure `git://`, GUI hosts backed by ready service endpoints and
   reachable through the app VIP, and critical Argo CD / Woodpecker ClusterIP
   service paths reachable from every node host and from diagnostic pods pinned
   to every node.
10. Supply-chain drift check: repository validation rejects mutable explicit
    image or chart tags such as `latest`, `next`, `nightly`, `dev`, or branch
    style tags in curated GitOps app manifests. `renovate.json` enables
    Renovate dependency dashboards, Helm grouping, and Docker `pinDigests`;
    `policies/kyverno/verify-signed-images.example.yaml` provides a stable
    copyable baseline, while the premium `platform-image-integrity` Application
    enforces Cosign verification after CI signs platform images and the live
    admission canary passes.

Use `docs/PRODUCTION_READINESS.md` as the final go/no-go checklist for live
gates, private evidence, exceptions, launch decision, and post-launch
validation.

Run the app-health gate with:

```bash
make platform-app-health
```

When only the GitOps/CI control plane is under repair, use the focused gate:

```bash
make platform-ci-health
```

It verifies Argo CD runtime pods, configured repo-server/Redis service
endpoints, URL routing, Traefik, Woodpecker server and agent HA replica
coverage, generated Woodpecker secrets, the running Woodpecker image tag, and
Argo CD / Woodpecker ClusterIP service paths without requiring Harbor, Grafana,
Prometheus, Loki, Velero, CloudNativePG, or Longhorn runtime checks to pass in
the same run. It sets `PLATFORM_APP_HEALTH_INCLUDE_EXISTING_APPS=false`, so
unrelated existing Argo CD Applications do not block this focused repair check.
For a read-only snapshot, `make platform-status` also reports Woodpecker
server/agent readiness, image tag drift, and per-GUI HTTPS status through the
app VIP from both the cluster side and the Ansible controller/client side before
the hard health gate runs.

If Woodpecker is `Synced` but still `Progressing`, or agents are running an old
`next-*` image after the private values were corrected, run:

```bash
make platform-woodpecker-repair
```

That reconciles Woodpecker's focused secrets and private seed source before
Argo CD service repair, then syncs the application, waits for the server and
agents, verifies the running image tags, and refreshes service-path consumers
after the strict rollout gate before `make platform-ci-health`. Use
`make platform-app-secrets` for the full production secret gate, including
Harbor S3 and backup credentials.

Before final production registration, prove that the selected profile has no
unresolved placeholders:

```bash
PLATFORM_PROFILE=premium-3node make platform-profile-check
```

This check must pass before treating the platform app layer as production-ready.

For the full read-only production readiness gate, run:

```bash
make platform-production-check
```

That command chains repository and supply-chain validation, the selected
GitOps profile placeholder check, RKE2 verification, platform status, live
capacity, network-isolation, internal-TLS, observability, and a production-mode
platform app health gate. Production mode remains strict even when the normal
health runner loads `private/seed-git.env` in bootstrap mode. Bootstrap mode
accepts temporary seed Git and only a verified uninitialized/sealed OpenBao
server while its private initialization ceremony is pending. For a one-off
bootstrap probe without the private env file, set
`PLATFORM_APP_HEALTH_FORBID_TEMPORARY_REPO=false` and
`PLATFORM_APP_HEALTH_OPENBAO_READY=false`; never use those overrides as
production evidence.

The app health gate requires controller/client access through the app VIP,
production-safe Argo CD repository sources instead of temporary seed Git or
insecure `git://`, exact repository matching when
`PLATFORM_APP_HEALTH_EXPECTED_REPO_URL` or `PLATFORM_REPO_URL` is set,
HTTP-to-HTTPS redirects for configured GUI hosts, ready GUI ingress backend
endpoints, required Longhorn StorageClasses, Longhorn node and
volume runtime health, critical HA replica coverage for Argo CD HA, Traefik,
and Woodpecker, Woodpecker server/agent runtime image tag alignment, Argo CD
runtime component and configured repo-server/Redis service endpoint coverage,
and Argo CD / Woodpecker ClusterIP service reachability from every RKE2 node
host and from diagnostic pods pinned to every RKE2 node. It also fails if
platform PVCs are Pending, Lost, or stuck Terminating. CloudNativePG checks
default to `auto`: any existing PostgreSQL clusters are verified, and
operator-only bootstrap installs are allowed. To make a cluster mandatory:

Temporary seed Git is bootstrap-only. For final production proof, Argo CD
Applications must point at the intended private Git service rather than
`git://...:9418` seed URLs. To also require one exact private source URL, run
the production gate with the same `PLATFORM_REPO_URL` used to register the
Applications, or set the explicit health variable:

```bash
PLATFORM_REPO_URL=<PRIVATE_REPO_URL> make platform-production-check
PLATFORM_APP_HEALTH_EXPECTED_REPO_URL=<PRIVATE_REPO_URL> make platform-app-health
```

During bootstrap-only troubleshooting, bypass just the seed/insecure source
check with:

```bash
PLATFORM_APP_HEALTH_FORBID_TEMPORARY_REPO=false make platform-app-health
```

```bash
PLATFORM_APP_HEALTH_CNPG_CLUSTERS="platform-databases/platform-postgres" make platform-app-health
```

cert-manager and trust-manager resource checks also default to `auto`: any
existing `Certificate` resources must be `Ready`, and any existing
trust-manager `Bundle` resources must be synced. To make exact resources
mandatory:

```bash
PLATFORM_APP_HEALTH_CERTIFICATES="argocd/argocd-server-tls" make platform-app-health
PLATFORM_APP_HEALTH_TRUST_BUNDLES="platform-public-roots" make platform-app-health
```

When step-ca is required, the health gate probes its in-cluster HTTPS
`/health` endpoint through the ClusterIP service. Temporary bypass:

```bash
PLATFORM_APP_HEALTH_STEP_CA_API=false make platform-app-health
```

When Harbor is required, `platform-app-health` also probes
`https://<registry-host>/v2/` through the app VIP and requires the Docker Distribution
API header, so a working UI alone is not treated as registry readiness. To skip
only that temporary check:

```bash
PLATFORM_APP_HEALTH_REGISTRY_API=false make platform-app-health
```

When Grafana or Prometheus are required, the same gate verifies Grafana
`/api/health` and Prometheus `/-/ready` through the app VIP. To skip only those
temporary API checks:

```bash
PLATFORM_APP_HEALTH_MONITORING_API=false make platform-app-health
```

When Loki or Velero are required, the gate also verifies a known Loki `/ready`
service endpoint and requires Velero `BackupStorageLocation` objects to be
`Available` plus at least one enabled Velero backup schedule. It also verifies
the generated app secret contracts for Harbor, Forgejo, Woodpecker, Keycloak,
Grafana, Loki, Velero, CloudNativePG, and Valkey
when those apps are required, checking that the expected Secret objects exist
with the required keys. Temporary bypasses:

```bash
PLATFORM_APP_HEALTH_LOKI_API=false make platform-app-health
PLATFORM_APP_HEALTH_VELERO_BACKUP_STORAGE=false make platform-app-health
PLATFORM_APP_HEALTH_VELERO_SCHEDULES=false make platform-app-health
PLATFORM_APP_HEALTH_APP_SECRETS=skip make platform-app-health
```

For production Harbor, also enforce the external PostgreSQL, Redis, and
registry S3 credential secrets:

```bash
PLATFORM_APP_HEALTH_HARBOR_PRODUCTION_SECRETS=true make platform-app-health
```

For production Forgejo, also enforce the SQL password secret and, when
`FORGEJO_REDIS_MODE=redis`, the Redis URI secret:

```bash
PLATFORM_APP_HEALTH_FORGEJO_PRODUCTION_SECRETS=true make platform-app-health
```

For production Grafana with external PostgreSQL, also enforce the database
password secret:

```bash
PLATFORM_APP_HEALTH_GRAFANA_DATABASE_SECRET=true make platform-app-health
```

Keycloak admin/database secrets are checked automatically when Keycloak is part
of the required app list. For custom secret names, set the same names used by
`platform-app-secrets` and
the private values renderer:

```bash
HARBOR_ADMIN_SECRET_NAME=harbor-admin \
HARBOR_SECRET_KEY_SECRET_NAME=harbor-secret-key \
HARBOR_DATABASE_SECRET_NAME=harbor-database \
HARBOR_REDIS_SECRET_NAME=harbor-redis \
HARBOR_S3_SECRET_NAME=harbor-registry-s3 \
FORGEJO_DATABASE_SECRET_NAME=forgejo-database \
FORGEJO_REDIS_SECRET_NAME=forgejo-redis \
WOODPECKER_FORGEJO_OAUTH_SECRET_NAME=woodpecker-forgejo-oauth \
WOODPECKER_DATABASE_SECRET_NAME=woodpecker-database \
GRAFANA_ADMIN_SECRET_NAME=grafana-admin \
GRAFANA_DATABASE_SECRET_NAME=grafana-database \
LOKI_OBJECT_STORAGE_SECRET_NAME=loki-object-storage \
VELERO_CREDENTIALS_SECRET_NAME=velero-credentials \
CNPG_OBJECT_STORE_SECRET_NAME=cnpg-object-store \
PLATFORM_APP_HEALTH_CNPG_OBJECT_STORAGE_SECRET=true \
make platform-app-health
```

Temporary certificate/trust bypasses:

```bash
PLATFORM_APP_HEALTH_CERTIFICATES=skip make platform-app-health
PLATFORM_APP_HEALTH_TRUST_BUNDLES=skip make platform-app-health
```

If a node-specific ClusterIP path fails, such as Woodpecker agents timing out on
the server gRPC port, repair the shared service path and rerun the health gate.
The repair also refreshes Woodpecker agents and verifies the Woodpecker gRPC
ClusterIP from every RKE2 node host and from diagnostic pods pinned to every
RKE2 node:

```bash
make platform-service-path-repair
make platform-app-health
```

Node-originated app VIP self-probes are advisory by default; enforce them with:

```bash
PLATFORM_APP_HEALTH_NODE_INGRESS_STRICT=true make platform-app-health
```

The pod-pinned service-path probe uses `rancher/klipper-helm:v0.10.0-build20260513`
by default because the bootstrap flow already pulls it for DNS diagnostics. For
restricted registries or slow pulls, override the image or timeout:

```bash
PLATFORM_APP_HEALTH_SERVICE_CHECK_IMAGE=<internal-image-with-curl-or-wget> \
PLATFORM_APP_HEALTH_SERVICE_CHECK_TIMEOUT=300 \
make platform-app-health
```

To skip required StorageClass enforcement during a temporary non-Longhorn subset
debug run:

```bash
PLATFORM_APP_HEALTH_STORAGE_CLASSES=skip make platform-app-health
```

To skip only Longhorn runtime node/volume enforcement during a temporary storage
repair run:

```bash
PLATFORM_APP_HEALTH_LONGHORN_RUNTIME=false make platform-app-health
```

To skip only Argo CD runtime component and configured repo-server/Redis service
endpoint enforcement during a temporary control-plane repair run:

```bash
PLATFORM_APP_HEALTH_ARGOCD_RUNTIME=false make platform-app-health
```

To skip only critical HA replica enforcement during a temporary scale or repair
run:

```bash
PLATFORM_APP_HEALTH_HA_REPLICAS=false make platform-app-health
```

After an intentional Woodpecker upgrade, set the expected runtime image tag:

```bash
PLATFORM_APP_HEALTH_WOODPECKER_IMAGE_TAG=v3.16.0 make platform-app-health
```

To skip only HTTP-to-HTTPS redirect enforcement during a temporary debug run:

```bash
PLATFORM_APP_HEALTH_HTTP_REDIRECT=false make platform-app-health
```

For an intentional subset of the premium stack, override all three enforced
lists together so app health, namespace readiness, and GUI route checks describe
the same target state:

```bash
PLATFORM_APP_HEALTH_REQUIRED_APPS="cert-manager trust-manager metallb traefik longhorn cloudnativepg platform-postgres platform-valkey forgejo woodpecker" \
PLATFORM_APP_HEALTH_NAMESPACES="argocd cert-manager cnpg-system platform-databases platform-cache forgejo woodpecker longhorn-system metallb-system traefik" \
PLATFORM_APP_HEALTH_GUI_APPS="argocd forgejo woodpecker" \
make platform-app-health
```
