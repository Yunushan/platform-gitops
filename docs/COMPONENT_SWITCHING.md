# Component Switching

This project is designed to be changed without restructuring the repository.

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

## Switch ingress-nginx to Traefik

Use profile:

```text
profiles/ingress-traefik.yaml
```

## Switch kube-vip to HAProxy/Keepalived

Use profile:

```text
profiles/vip-haproxy-keepalived.yaml
```
