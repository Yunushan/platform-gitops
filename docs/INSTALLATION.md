# Installation Guide

## Prerequisites

- Three Linux nodes for RKE2 server mode.
- A node operating system from `docs/NODE_OS_SUPPORT.md`.
- A virtual IP or DNS name for the Kubernetes API endpoint.
- SSH access from an admin workstation.
- Git, kubectl, Helm, and basic shell tools.
- Off-cluster backup location.

For the premium profile, prefer SLES, RHEL, Rocky Linux, Oracle Linux, or Ubuntu Server LTS. Debian, AlmaLinux, CentOS Stream, Fedora, Arch, Gentoo, and Linux Mint are documented as compatible or lab/workstation targets where upstream validation is limited.

## Step 1: Prepare local configuration

```bash
make init-local
```

Edit:

```text
config/cluster.local.yaml
inventory/hosts.local.ini
```

## Step 2: Configure VIP

Default: kube-vip.

Alternative examples are stored in:

```text
scripts/vip/haproxy.cfg.example
scripts/vip/keepalived.conf.example
```

## Step 3: Install RKE2

On the first server node:

```bash
sudo RKE2_TOKEN=<GENERATE_WITH_PASSWORD_MANAGER>   RKE2_API_ENDPOINT=<VIP_DNS_NAME>   scripts/bootstrap/install-rke2-first-server.sh
```

On the second and third server nodes:

```bash
sudo RKE2_TOKEN=<SAME_PRIVATE_TOKEN>   RKE2_API_ENDPOINT=<VIP_DNS_NAME>   scripts/bootstrap/install-rke2-server.sh
```

Never store the real token in git.

## Step 4: Bootstrap Argo CD

```bash
export PLATFORM_REPO_URL=<THIS_REPO_URL>
export KUBECONFIG=<PATH_TO_PRIVATE_KUBECONFIG>
scripts/bootstrap/bootstrap-argocd.sh
```

## Step 5: Let GitOps take over

Argo CD reads:

```text
gitops/bootstrap/root-app.yaml
gitops/clusters/rke2-main/platform-apps.yaml
```

For the premium 3-node profile, use:

```text
gitops/bootstrap/root-app-premium-3node.yaml
gitops/clusters/rke2-main/premium-3node/platform-apps.yaml
```

Review each platform application before enabling automatic sync in production.
