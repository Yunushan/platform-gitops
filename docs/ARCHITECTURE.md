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
| CI | Woodpecker CI |
| CD | Argo CD HA |
| Registry | Harbor |
| Database | CloudNativePG PostgreSQL |
| Storage | Longhorn default, Rook/Ceph alternative |
| LoadBalancer services | MetalLB |
| Ingress | Traefik default, ingress-nginx alternative |
| TLS | cert-manager |
| Monitoring | Prometheus + Grafana |
| Logs | Loki |
| Backups | Velero plus database and off-cluster backups |
| Secrets | SOPS + age default, External Secrets/OpenBao option |
| Policy | Kyverno examples |
| Supply chain | Cosign and Renovate helpers |

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

## Security model

- No production secret is committed to git.
- Real environment data exists only in ignored local files or external secret systems.
- Production deployment is driven by Argo CD from GitOps repositories.
- CI builds artifacts and updates desired state; it does not directly deploy production.

## Failure model

A 3-node cluster is designed for one-node failure. It is not a replacement for multi-site disaster recovery. Off-cluster backups are mandatory.
