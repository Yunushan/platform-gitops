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

## Use the premium 3-node GitLab CE profile for 100+ users

Use profile:

```text
profiles/premium-3node-gitlab-ce.yaml
```

Bootstrap by selecting the profile at registration time:

```bash
PLATFORM_PROFILE=premium-3node-gitlab-ce PLATFORM_APPLY_GITOPS=true PLATFORM_REPO_URL=<PRIVATE_REPO_URL> make platform-argocd
```

The profile inherits the premium 3-node platform, removes the Forgejo and Woodpecker applications, and adds the premium GitLab apps:

```text
gitops/clusters/rke2-main/premium-3node/apps/gitlab-ce
gitops/clusters/rke2-main/premium-3node/apps/gitlab-runner
```

The GitLab values are a starting budget for 100+ users on three 8-vCPU, 32-GiB nodes: two to three webservice, Sidekiq, and gitlab-shell replicas; one Gitaly replica with a 200Gi `longhorn-critical-encrypted` volume; external CloudNativePG PostgreSQL; TLS-verified platform Valkey; external S3-compatible object storage for artifacts, LFS, uploads, backups, and the registry; and up to 20 concurrent runner jobs. Load-test the profile and record the result in the private capacity baseline before production use.

Before production registration:

- Provision the `gitlabhq_production` database and `gitlab` role in `platform-postgres`.
- Create the `gitlab-postgres`, `gitlab-redis`, `gitlab-redis-ca`, `gitlab-object-storage`, and `gitlab-registry-storage` secrets and the `gitlab-tls` certificate outside Git. Replace every `<...>` placeholder through the private values flow.
- Create the runner registration credential through a private overlay or ExternalSecret.
- Run `PLATFORM_PROFILE=premium-3node-gitlab-ce make platform-profile-check` after rendering private values.

Known limits: Gitaly is a single replica and is not a Praefect-backed HA repository layer, so repository availability depends on node recovery and tested Longhorn/Velero restores. GitLab SSO through Keycloak is not configured by this profile.

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
