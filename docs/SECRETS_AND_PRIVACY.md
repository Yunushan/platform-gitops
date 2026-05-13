# Secrets and Privacy

## What must never be committed

```text
Real IP addresses
Real internal domains
Real company or customer names
Passwords
Tokens
API keys
SSH private keys
TLS private keys
Kubeconfigs
Database credentials
Cloud credentials
Backup credentials
```

## Safe placeholder style

Use placeholders like:

```text
<NODE_1_IP>
<VIP_ADDRESS>
<PLATFORM_DOMAIN>
<GENERATE_WITH_PASSWORD_MANAGER>
<OFF_CLUSTER_BACKUP_TARGET>
```

## Recommended secret patterns

Choose one:

1. SOPS with age.
2. Sealed Secrets.
3. External Secrets Operator.
4. Vault or another private secret manager.
5. Manual secrets for small lab use only.

## Local files

Only local ignored files should contain real values:

```text
config/cluster.local.yaml
inventory/hosts.local.ini
.env
```

## Required checks

```bash
make no-secrets
```

This scanner is intentionally conservative. Use a professional scanner in production pipelines too.
