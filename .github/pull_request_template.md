## Summary

- What changed:
- Why it changed:

## Change Type

- [ ] Documentation only
- [ ] Validation or tooling
- [ ] GitOps manifest/profile values
- [ ] Bootstrap or Ansible automation
- [ ] Security, policy, or supply-chain behavior
- [ ] Production operations, alerting, backup, or restore guidance

## Public-Safety Check

- [ ] No real domains, private IPs, customer names, usernames, tokens, passwords, kubeconfigs, SSH keys, TLS keys, or age private keys.
- [ ] New examples use placeholders or safe example values only.
- [ ] Secret material is documented as SOPS, External Secrets, Sealed Secrets, Vault/OpenBao, or out-of-band Kubernetes Secrets.

## Validation

- [ ] `python scripts/run_validation.py`
- [ ] `make no-secrets` or `python scripts/validate_no_secrets.py`
- [ ] Additional focused check:

## Production Impact

- [ ] No live-cluster behavior changes.
- [ ] Affected components:
- [ ] Required private deployment action:
- [ ] Required maintenance window:
- [ ] Required restore, alerting, security, or operations evidence update:

## Rollback

- Revert plan:
- Data-loss risk:
- Manual cleanup required:

## Documentation

- [ ] README/docs updated when behavior, commands, targets, profiles, or production guidance changed.
- [ ] `SECURITY.md`, `docs/OPERATIONS.md`, `docs/ALERTING.md`, or `docs/BACKUP_RESTORE.md` updated when relevant.
