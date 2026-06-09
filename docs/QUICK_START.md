# Quick Start

This quick start creates a safe local workspace and prepares the 3-node platform plan.

## 1. Clone and initialize

```bash
git clone <YOUR_REMOTE_URL> platform-gitops
cd platform-gitops
make init-local
```

## 2. Edit local-only files

```bash
${EDITOR:-vi} config/cluster.local.yaml
${EDITOR:-vi} inventory/hosts.local.ini
```

Do not edit real IPs, tokens, or company data into tracked files.

## 3. Validate before push

```bash
make validate
make no-secrets
```

## 4. Bootstrap plan

```bash
make bootstrap-plan
```

Prepare and install RKE2 through Ansible:

```bash
make rke2-prepare
make rke2-install
```

Optional exact RKE2 version pin:

```bash
RKE2_VERSION='v1.35.4+rke2r1' make rke2-install
```

## 5. Deploy order

1. Prepare three Rocky Linux 10 nodes.
2. Configure API VIP.
3. Install RKE2 with Cilium on three server nodes.
4. Install Argo CD HA bootstrap.
5. Apply `gitops/bootstrap/root-app.yaml` with your private repository URL substituted at runtime.
6. Let Argo CD deploy Traefik and the platform components.
7. Configure off-cluster backups.
8. Run a restore drill.
