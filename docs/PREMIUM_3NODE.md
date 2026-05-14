# Premium 3-Node Profile

The premium profile keeps the compact 3-node RKE2 footprint, but uses stricter defaults for production-like private deployments.

## What changes

| Area | Default profile | Premium profile |
|---|---|---|
| Bootstrap path | `gitops/clusters/rke2-main` | `gitops/clusters/rke2-main/premium-3node` |
| Ingress | ingress-nginx | Traefik |
| Storage | Minimal Longhorn values | Longhorn storage classes and backup target placeholders |
| Registry | Basic Harbor values | Harbor HA values with external PostgreSQL, Redis, and object storage placeholders |
| Database | CloudNativePG operator only | CloudNativePG operator plus a 3-instance cluster example with WAL archive |
| Backups | Velero placeholders | Velero schedule, CSI snapshot, and off-cluster object storage placeholders |
| Observability | Minimal values | HA Prometheus, Alertmanager, Grafana, Loki, and service monitors |
| Security | Optional policy examples | Additional NetworkPolicy and Kyverno examples |

## Activation

Use the premium root app instead of the default root app when bootstrapping Argo CD:

```text
gitops/bootstrap/root-app-premium-3node.yaml
```

That root app points Argo CD at:

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
