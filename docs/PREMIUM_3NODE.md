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
| CI | Woodpecker CI | Woodpecker server replicas plus 3 Kubernetes agents |
| CD / GitOps | Argo CD HA | Argo CD HA with multi-replica server, repo server, ApplicationSet, and Redis HA |
| Backups | Velero placeholders | Velero schedule, CSI snapshot, and off-cluster object storage placeholders |
| Observability | Minimal values | HA Prometheus, Alertmanager, Grafana, Loki, and service monitors |
| Security | Optional policy examples | Additional NetworkPolicy and Kyverno examples |

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
- Woodpecker OAuth and agent settings.
- Grafana, Prometheus, and Loki storage sizes.

`make platform-render-private-values` can render first-deploy private values
for Forgejo, Argo CD, Woodpecker, Harbor, Grafana, Prometheus, Loki, Velero,
Longhorn, and optional step-ca from `inventory/hosts.local.ini` plus
environment variables. Runtime credentials still stay out of Git:
`make platform-app-secrets` can create the Woodpecker PostgreSQL datasource,
Loki, and Velero Kubernetes secrets from `WOODPECKER_DATABASE_DATASOURCE`,
`WOODPECKER_DATABASE_HOST` / `WOODPECKER_DATABASE_PASSWORD`,
`LOKI_S3_ACCESS_KEY_ID` / `LOKI_S3_SECRET_ACCESS_KEY`,
`VELERO_CLOUD_CREDENTIALS`, or `AWS_ACCESS_KEY_ID` /
`AWS_SECRET_ACCESS_KEY`. For production, set
`PLATFORM_APP_SECRET_REQUIRE_OBJECT_STORAGE=true` so missing Loki or Velero
object-storage credential secrets fail during secret automation instead of
surfacing later as unhealthy pods.

## CI/CD high availability

The premium profile keeps Forgejo as the Git forge and uses Woodpecker plus Argo CD for CI/CD. Forgejo may remain single-replica until repository storage, SSH access, and restore procedures are proven, but that does not mean CI/CD is single-node.

Woodpecker is configured for the 3-node cluster with:

- `server.statefulSet.replicaCount: 2` for the Woodpecker web/API service.
- `agent.replicas: 3` for Kubernetes-backed build agents.
- Explicit server and agent image repositories plus `WOODPECKER_IMAGE_TAG`, defaulting to `3.16.0`.
- PostgreSQL-backed state through `WOODPECKER_DATABASE_DRIVER=postgres`.
- Traefik ingress at the effective CI hostname, defaulting to `woodpecker.<PLATFORM_DOMAIN>` unless `platform_ci_host` or `platform_woodpecker_host` is set.

The first-deploy renderer defaults Woodpecker to single-server SQLite so the
dashboard can come online before a PostgreSQL DSN exists. Keep
`WOODPECKER_SERVER_REPLICAS=1` while `WOODPECKER_DATABASE_MODE=sqlite`. It also
pins both Woodpecker server and agent image repositories plus
`WOODPECKER_IMAGE_TAG`, defaulting to `3.16.0`; change that only as an intentional upgrade. For production HA,
provide either `WOODPECKER_DATABASE_DATASOURCE` or `WOODPECKER_DATABASE_HOST`
plus `WOODPECKER_DATABASE_PASSWORD`, let `platform-app-secrets` create the
`woodpecker-database` secret, then render with:

```bash
WOODPECKER_DATABASE_DATASOURCE='postgres://woodpecker:<PASSWORD>@<POSTGRES_HOST>:5432/woodpecker?sslmode=disable' \
WOODPECKER_DATABASE_SECRET_NAME=woodpecker-database \
PLATFORM_APP_SECRET_REQUIRE_WOODPECKER_DATABASE=true \
make platform-app-secrets

WOODPECKER_DATABASE_MODE=postgres \
WOODPECKER_DATABASE_SECRET_NAME=woodpecker-database \
WOODPECKER_IMAGE_TAG=3.16.0 \
WOODPECKER_SERVER_REPLICAS=2 \
WOODPECKER_AGENT_REPLICAS=3 \
make platform-render-private-values
```

Argo CD HA is configured with:

- `server.replicas: 3` in the premium values.
- `repoServer.replicas: 3` in the premium values.
- `applicationSet.replicas: 2`.
- `redis-ha.enabled: true`.

With the 3 RKE2 server nodes healthy, these services are scheduled across the cluster and continue operating through a single-node failure, assuming storage, database, DNS, ingress VIP, and off-cluster backups are correctly configured.

## Storage stance

The premium profile keeps Longhorn instead of switching to Rook/Ceph. For only three nodes, Longhorn is the more practical default. The profile creates:

- `longhorn-standard` with 2 replicas for ordinary stateful workloads.
- `longhorn-critical` with 3 replicas for critical platform state.
- `longhorn-cache` with 1 replica for rebuildable cache data.

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
    style tags in curated GitOps app manifests.

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

That hard-refreshes and syncs the Woodpecker Argo CD application, waits for the
server and agents, verifies the running server and agent image tags, and runs
service-path consumer refresh before and after the strict Woodpecker rollout
gate before `make platform-ci-health`.

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

That command chains repository validation, the selected GitOps profile
placeholder check, RKE2 verification, platform status, and the platform app
health gate.

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
the generated app secret contracts for Harbor, Woodpecker, Loki, and Velero
when those apps are required, checking that the expected Secret objects exist
with the required keys. Temporary bypasses:

```bash
PLATFORM_APP_HEALTH_LOKI_API=false make platform-app-health
PLATFORM_APP_HEALTH_VELERO_BACKUP_STORAGE=false make platform-app-health
PLATFORM_APP_HEALTH_VELERO_SCHEDULES=false make platform-app-health
PLATFORM_APP_HEALTH_APP_SECRETS=skip make platform-app-health
```

For custom secret names, set the same names used by `platform-app-secrets` and
the private values renderer:

```bash
HARBOR_ADMIN_SECRET_NAME=harbor-admin \
HARBOR_SECRET_KEY_SECRET_NAME=harbor-secret-key \
WOODPECKER_FORGEJO_OAUTH_SECRET_NAME=woodpecker-forgejo-oauth \
WOODPECKER_DATABASE_SECRET_NAME=woodpecker-database \
LOKI_OBJECT_STORAGE_SECRET_NAME=loki-object-storage \
VELERO_CREDENTIALS_SECRET_NAME=velero-credentials \
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
PLATFORM_APP_HEALTH_WOODPECKER_IMAGE_TAG=3.16.0 make platform-app-health
```

To skip only HTTP-to-HTTPS redirect enforcement during a temporary debug run:

```bash
PLATFORM_APP_HEALTH_HTTP_REDIRECT=false make platform-app-health
```

For an intentional subset of the premium stack, override all three enforced
lists together so app health, namespace readiness, and GUI route checks describe
the same target state:

```bash
PLATFORM_APP_HEALTH_REQUIRED_APPS="cert-manager trust-manager metallb traefik longhorn cloudnativepg forgejo woodpecker" \
PLATFORM_APP_HEALTH_NAMESPACES="argocd cert-manager cnpg-system forgejo woodpecker longhorn-system metallb-system traefik" \
PLATFORM_APP_HEALTH_GUI_APPS="argocd forgejo woodpecker" \
make platform-app-health
```
