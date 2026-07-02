# Contributing

Thank you for improving Platform GitOps Workspace.

## Rules

1. Do not commit real IP addresses, domains, credentials, kubeconfigs, SSH keys, TLS keys, tokens, or customer/company names.
2. Use placeholders such as `<NODE_1_IP>`, `<PLATFORM_DOMAIN>`, and `<GENERATE_WITH_PASSWORD_MANAGER>`.
3. Run `make validate` and `make no-secrets` before opening a pull request. If `make` is not installed, run `python scripts/run_validation.py`.
4. Keep defaults zero-subscription and self-hostable.
5. Document every operational change in `docs/`.

## Pull request checklist

- [ ] No private data committed.
- [ ] No plaintext secrets committed.
- [ ] README or docs updated.
- [ ] Profiles updated when component defaults change.
- [ ] Validation passes.
