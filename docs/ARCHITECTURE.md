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
- Production deployment is driven by Argo CD from GitOps repositories.
- CI builds artifacts and updates desired state; it does not directly deploy production.

## Failure model

A 3-node cluster is designed for one-node failure. It is not a replacement for multi-site disaster recovery. Off-cluster backups are mandatory.
