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

## SOPS with age starter

Use `config/sops.age.example.yaml` as the safe starter policy for private
deployment repositories:

```bash
age-keygen -o ~/.config/sops/age/keys.txt
cp config/sops.age.example.yaml .sops.yaml
```

Then replace `age1REPLACE_WITH_PUBLIC_AGE_RECIPIENT` with the public recipient
printed by `age-keygen`. The public recipient can live in `.sops.yaml`; the
private age key must stay outside Git in a password manager, CI secret store, or
operator workstation key store. Keep real `.sops.yaml` files private when they
encode internal repository layout or recipient policy.

The example encrypts common secret-bearing keys under private/rendered secret
paths, ignored local config, and GitOps files whose names indicate secrets,
credentials, or datasources. Review the rules with your security team before
using them for production.

## Local files

Only local ignored files should contain real values:

```text
config/cluster.local.yaml
inventory/hosts.local.ini
.env
```

Organization-private deployment repositories may contain internal FQDNs and
safe non-secret sizing/routing values. First private deploy flows set
`PLATFORM_NO_SECRETS_ALLOW_INTERNAL_HOSTNAMES=true` for that reason. Do not use
that allowance for public template validation or for any sync that might push
rendered private values back to a public source remote. Even with the allowance,
the scanner still blocks plaintext secrets, private keys, kubeconfigs, and
private IPs.

## Required checks

```bash
make no-secrets
```

If Python is installed under a non-default path, pass it explicitly:

```bash
PYTHON=/path/to/python make validate
PYTHON=/path/to/python make platform-argocd
```

This scanner is intentionally conservative. Use a professional scanner in production pipelines too.

For component-level data classes, retention decisions, disposal, and private
evidence expectations, use `docs/DATA_CLASSIFICATION.md`.
