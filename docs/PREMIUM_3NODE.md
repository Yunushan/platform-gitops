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
environment variables. Object-storage credentials still stay out of Git:
`make platform-app-secrets` can create the Loki and Velero Kubernetes secrets
from `LOKI_S3_ACCESS_KEY_ID` / `LOKI_S3_SECRET_ACCESS_KEY`,
`VELERO_CLOUD_CREDENTIALS`, or `AWS_ACCESS_KEY_ID` /
`AWS_SECRET_ACCESS_KEY`. For production, set
`PLATFORM_APP_SECRET_REQUIRE_OBJECT_STORAGE=true` so missing Loki or Velero
object-storage credential secrets fail during secret automation instead of
surfacing later as unhealthy pods.

## CI/CD high availability

The premium profile keeps Forgejo as the Git forge and uses Woodpecker plus Argo CD for CI/CD. Forgejo may remain single-replica until repository storage, SSH access, and restore procedures are proven, but that does not mean CI/CD is single-node.

Woodpecker is configured for the 3-node cluster with:

- `server.replicas: 2` for the Woodpecker web/API service.
- `agent.replicas: 3` for Kubernetes-backed build agents.
- PostgreSQL-backed state through `WOODPECKER_DATABASE_DRIVER=postgres`.
- Traefik ingress at the effective CI hostname, defaulting to `woodpecker.<PLATFORM_DOMAIN>` unless `platform_ci_host` or `platform_woodpecker_host` is set.

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
   StorageClasses present, platform PVCs Bound, GUI hosts backed by ready
   service endpoints and reachable through the app VIP, and critical Argo CD /
   Woodpecker ClusterIP service paths reachable from every node host and from
   diagnostic pods pinned to every node:

```bash
make platform-app-health
```

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
HTTP-to-HTTPS redirects for configured GUI hosts, ready GUI ingress backend
endpoints, required Longhorn StorageClasses, and Argo CD / Woodpecker ClusterIP
service reachability from every RKE2 node host and from diagnostic pods pinned
to every RKE2 node. It also fails if platform PVCs are Pending, Lost, or stuck
Terminating. Node-originated app VIP self-probes are advisory by default;
enforce them with:

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
