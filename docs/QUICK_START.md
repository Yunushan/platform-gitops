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

For a real company installation, use this public repository as the MIT template
and keep the actual cluster desired state in a private deployment repository.
See `docs/PRIVATE_DEPLOYMENT.md`.

## 3. Validate before push

```bash
make validate
make no-secrets
```

This is a repository-only check. It needs Python 3 and Bash, and it does not contact a live cluster. If `make` is not installed, run
`python scripts/run_validation.py` instead. The live cluster proof happens later
with `make platform-production-check`.

## 4. Bootstrap plan

```bash
make bootstrap-plan
```

Prepare and install RKE2 through Ansible:

```bash
make rke2-preflight
make rke2-prepare
make rke2-install
```

Optional exact RKE2 version pin:

```bash
RKE2_VERSION='v1.35.4+rke2r1' make rke2-install
```

## 5. Deploy order

1. Prepare three Rocky Linux 10 nodes.
2. Configure API VIP and ingress VIP.
3. Run `make rke2-preflight`.
4. Install RKE2 with Cilium on three server nodes.
5. Install Argo CD HA bootstrap.
6. Register the selected GitOps profile with `PLATFORM_REPO_URL=<PRIVATE_REPO_URL> PLATFORM_APPLY_GITOPS=true PLATFORM_PROFILE=premium-3node make platform-argocd`.
7. Let Argo CD deploy Traefik and the platform components.
8. Configure off-cluster backups.
9. Run `make platform-production-check`.
10. Complete the go/no-go checklist in `docs/PRODUCTION_READINESS.md`.
11. Run a restore drill using `docs/BACKUP_RESTORE.md`.
