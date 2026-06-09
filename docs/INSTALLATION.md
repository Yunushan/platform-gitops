# Installation Guide

## Prerequisites

- Three Linux nodes for RKE2 server mode.
- A node operating system from `docs/NODE_OS_SUPPORT.md`.
- A virtual IP or DNS name for the Kubernetes API endpoint.
- SSH access from an admin workstation.
- Git, kubectl, Helm, and basic shell tools.
- Off-cluster backup location.

The default recommendation is Rocky Linux 10 on all three nodes, with RKE2 using Cilium as the CNI. For the premium profile, Rocky Linux 10 remains the zero-subscription default; SLES, RHEL, Oracle Linux, and Ubuntu Server LTS are also suitable enterprise choices. Debian, AlmaLinux, CentOS Stream, Fedora, Arch, Gentoo, and Linux Mint are documented as compatible or lab/workstation targets where upstream validation is limited.

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

Recommended Ansible flow:

```bash
make rke2-prepare
make rke2-install
```

The install playbook reads these from `inventory/hosts.local.ini`:

```ini
[rke2_servers:vars]
rke2_api_vip=<VIP_ADDRESS>
rke2_api_dns=<VIP_DNS_NAME>
```

If `rke2_token` is omitted or left as a placeholder, the playbook generates a private controller-side token at:

```text
~/.config/platform-gitops/rke2-token
```

To pin an exact RKE2 version, use either environment variable style:

```bash
RKE2_VERSION='v1.35.4+rke2r1' make rke2-install
```

or Ansible extra vars:

```bash
ansible-playbook -i inventory/hosts.local.ini ansible/playbooks/install-rke2.yml \
  -e rke2_version='v1.35.4+rke2r1'
```

If no version is pinned, the playbook uses the configured channel:

```ini
rke2_channel=stable
```

The playbook defaults to:

```text
rke2_cni=cilium
```

Override `rke2_cni` only if you intentionally choose another supported RKE2 CNI.

If the API VIP is not active yet, temporarily point joining servers at node-1 while keeping the API VIP in TLS SANs:

```bash
ansible-playbook -i inventory/hosts.local.ini ansible/playbooks/install-rke2.yml \
  -e rke2_join_endpoint=<NODE_1_IP>
```

Manual scripts remain available for debugging:

```bash
sudo RKE2_TOKEN=<TOKEN> RKE2_API_ENDPOINT=<VIP_DNS_NAME> RKE2_VERSION=<RKE2_VERSION> scripts/bootstrap/install-rke2-first-server.sh
sudo RKE2_TOKEN=<TOKEN> RKE2_API_ENDPOINT=<VIP_DNS_NAME> RKE2_VERSION=<RKE2_VERSION> scripts/bootstrap/install-rke2-server.sh
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
