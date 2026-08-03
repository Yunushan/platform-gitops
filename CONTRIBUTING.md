# Contributing

Thank you for improving Platform GitOps Workspace.

## Rules

1. Do not commit real IP addresses, domains, credentials, kubeconfigs, SSH keys, TLS keys, tokens, or customer/company names.
2. Use placeholders such as `<NODE_1_IP>`, `<PLATFORM_DOMAIN>`, and `<GENERATE_WITH_PASSWORD_MANAGER>`.
3. Run `make validate` and `make no-secrets` before opening a pull request. If `make` is not installed, run `python scripts/run_validation.py`.
4. Keep defaults zero-subscription and self-hostable.
5. Document every operational change in `docs/`.
6. Report suspected vulnerabilities through `SECURITY.md`; do not include private exploit details or real deployment data in public issues.
7. Use the pull request and issue templates under `.github/` so production impact, rollback, validation, and public-safety checks are visible during review.
8. For private deployments, copy `.github/CODEOWNERS.example` to `.github/CODEOWNERS`, replace the `@org/...` placeholders with real owners, and enable branch protection with required checks and conversation resolution. Routine PR merging may omit required reviews; production releases use the independent `production-release` reviewer gate.

## Pull request checklist

- [ ] No private data committed.
- [ ] No plaintext secrets committed.
- [ ] README or docs updated.
- [ ] Profiles updated when component defaults change.
- [ ] Validation passes.
- [ ] Production impact and rollback notes are filled in when behavior changes.
- [ ] CODEOWNERS routing is correct for sensitive areas; the independent production-release reviewer gate is configured for production promotion.
