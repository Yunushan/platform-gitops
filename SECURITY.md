# Security Policy

## Reporting a vulnerability

Open a private security report through your Git hosting platform or contact the maintainers using your internal security process.

Do not paste real secrets, tokens, private keys, production hostnames, internal IPs, or customer data into public issues.

## Repository secret policy

This repository must not contain live secrets. Use one of these patterns:

- SOPS with age or a cloud KMS.
- Sealed Secrets.
- External Secrets Operator.
- Vault or another private secret manager.
- Manual Kubernetes Secrets created outside git for small labs.

Run:

```bash
make no-secrets
```

before pushing changes.
