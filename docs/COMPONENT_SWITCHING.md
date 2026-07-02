# Component Switching

This project is designed to be changed without restructuring the repository.
Replacement profiles keep the shared platform services, such as cert-manager,
MetalLB, storage, database operator, registry, monitoring, logging, and
backups, unless the profile explicitly lists a component under `remove`.
Use the profile name without the `.yaml` suffix when bootstrapping:

```bash
PLATFORM_PROFILE=<profile-name> PLATFORM_APPLY_GITOPS=true PLATFORM_REPO_URL=<PRIVATE_REPO_URL> make platform-argocd
```

## Use the premium 3-node profile

Use profile:

```text
profiles/premium-3node.yaml
```

Bootstrap by selecting the profile at registration time:

```bash
PLATFORM_PROFILE=premium-3node PLATFORM_APPLY_GITOPS=true PLATFORM_REPO_URL=<PRIVATE_REPO_URL> make platform-argocd
```

This registers applications from:

```text
gitops/clusters/rke2-main/premium-3node
```

The premium path uses the same Rocky Linux 10, RKE2, Cilium, Traefik, Forgejo, Woodpecker, and Argo CD HA recommendation, then adds hardened values for Harbor, Longhorn, CloudNativePG, monitoring, Loki, and Velero.

## Switch Forgejo to Gitea

Use profile:

```text
profiles/gitea-woodpecker-argocd.yaml
```

Enable:

```text
gitops/clusters/rke2-main/alternatives/gitea
```

Disable:

```text
gitops/clusters/rke2-main/apps/forgejo
```

## Switch Forgejo/Woodpecker to GitLab CE

Use profile:

```text
profiles/gitlab-ce-runner-argocd.yaml
```

Enable:

```text
gitops/clusters/rke2-main/alternatives/gitlab-ce
gitops/clusters/rke2-main/alternatives/gitlab-runner
```

Disable:

```text
gitops/clusters/rke2-main/apps/forgejo
gitops/clusters/rke2-main/apps/woodpecker
```

## Switch Longhorn to Rook/Ceph

Use profile:

```text
profiles/storage-rook-ceph.yaml
```

## Switch Traefik to ingress-nginx

Use profile:

```text
profiles/ingress-nginx.yaml
```

## Switch kube-vip to HAProxy/Keepalived

Use profile:

```text
profiles/vip-haproxy-keepalived.yaml
```
